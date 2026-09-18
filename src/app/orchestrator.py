"""End-to-end orchestration of the optimize-energy pipeline."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.config import Settings
from app.metrics import (
    directive_validation_failures,
    llm_in_use,
    llm_latency_seconds,
    optimizer_latency_seconds,
    replay_failures_total,
)
from app.models import OptimizeRequest, OptimizeResponse
from app.replay import ReplayError, assert_replay
from llm.fallback import regex_parse
from llm.interpreter import LLMInterpreter, LLMConfig
from llm.validator import safe_validate_directives
from optimizer.directives import build_effective_arrays
from optimizer.scheduler import (
    OptimizationError,
    ScheduleResult,
    compose_response,
    optimize_schedule,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrchestratorResult:
    response: OptimizeResponse
    used_llm: bool
    used_fallback: bool
    llm_latency_seconds: float
    optimizer_latency_seconds: float
    total_latency_seconds: float
    source: str  # "llm" | "fallback"


class Orchestrator:
    """Coordinates interpret → validate → apply → optimize → replay."""

    def __init__(self, settings: Settings, *, interpreter: LLMInterpreter | None = None) -> None:
        self._settings = settings
        self._interpreter = interpreter or LLMInterpreter(
            LLMConfig(
                api_key=settings.openai_api_key,
                model=settings.openai_model,
                timeout_seconds=settings.openai_timeout_seconds,
                seed=settings.openai_seed,
                max_retries=settings.openai_max_retries,
            )
        )

    async def run(self, request: OptimizeRequest) -> OrchestratorResult:
        started = time.perf_counter()

        interpretations, used_llm, llm_seconds = await self._interpret(request)

        # Apply directives -> effective arrays.
        effective = build_effective_arrays(
            request.hours,
            interpretations,
            battery_capacity_kwh=request.battery.capacity_kwh,
            base_min_reserve_kwh=request.battery.minimum_energy_kwh,
        )

        # Solve LP.
        opt_started = time.perf_counter()
        try:
            result: ScheduleResult = optimize_schedule(
                request.hours,
                initial_energy_kwh=request.battery.initial_energy_kwh,
                capacity_kwh=request.battery.capacity_kwh,
                max_charge_kwh_per_hour=request.battery.max_charge_kwh_per_hour,
                max_discharge_kwh_per_hour=request.battery.max_discharge_kwh_per_hour,
                effective=effective,
                warm_start=self._settings.optimizer_warm_start,
                time_limit_seconds=self._settings.optimizer_time_limit_seconds,
            )
        except OptimizationError as e:
            optimizer_latency_seconds.labels(status="infeasible").observe(time.perf_counter() - opt_started)
            logger.error("optimizer infeasible: %s", e)
            raise
        optimizer_seconds = time.perf_counter() - opt_started
        optimizer_latency_seconds.labels(status=result.status).observe(optimizer_seconds)

        response = compose_response(
            scenario_id=request.scenario_id,
            interpretations=interpretations,
            result=result,
            used_llm=used_llm,
        )

        # Replay check.
        try:
            assert_replay(request, response)
        except ReplayError as e:
            replay_failures_total.inc()
            logger.error("replay check failed: %s", e)
            raise

        llm_in_use.set(1 if used_llm else 0)
        total_seconds = time.perf_counter() - started
        source = "llm" if used_llm else "fallback"
        logger.info(
            "optimize ok",
            extra={
                "scenario_id": request.scenario_id,
                "source": source,
                "llm_latency_s": llm_seconds,
                "optimizer_latency_s": optimizer_seconds,
                "total_latency_s": total_seconds,
                "total_cost_bdt": response.total_cost_bdt,
            },
        )
        return OrchestratorResult(
            response=response,
            used_llm=used_llm,
            used_fallback=not used_llm,
            llm_latency_seconds=llm_seconds,
            optimizer_latency_seconds=optimizer_seconds,
            total_latency_seconds=total_seconds,
            source=source,
        )

    # ---- internals --------------------------------------------------------

    async def _interpret(
        self, request: OptimizeRequest
    ) -> tuple[list, bool, float]:
        """Try LLM; fall back to regex parser on failure if enabled."""
        from app.resilience import build_circuit_breaker, with_fallback

        breaker = build_circuit_breaker(
            fail_max=self._settings.circuit_breaker_fail_max,
            reset_timeout=self._settings.circuit_breaker_reset_timeout,
        )

        async def _llm_path() -> tuple[list, float]:
            t0 = time.perf_counter()
            try:
                if not self._settings.openai_configured():
                    raise RuntimeError("OPENAI_API_KEY not configured")
                entries = await self._interpreter.interpret(request)
            finally:
                elapsed = time.perf_counter() - t0
                llm_latency_seconds.labels(outcome="success").observe(elapsed)
            return entries, elapsed

        async def _fallback_path() -> tuple[list, float]:
            t0 = time.perf_counter()
            raw = regex_parse(request.operator_notes)
            entries = safe_validate_directives(
                raw,
                n_notes=len(request.operator_notes),
                battery_capacity_kwh=request.battery.capacity_kwh,
            )
            elapsed = time.perf_counter() - t0
            llm_latency_seconds.labels(outcome="fallback").observe(elapsed)
            return entries, elapsed

        if not self._settings.enable_fallback_parser:
            # Even without the fallback parser, we still wrap with the breaker.
            entries, elapsed = await _llm_path()
            return entries, True, elapsed

        (entries, elapsed), source = await with_fallback(
            _llm_path,
            _fallback_path,
            breaker=breaker,
            label="llm_interpreter",
        )
        used_llm = source == "primary"
        if not used_llm:
            # Count validation failures observed via the fallback (typically zero).
            for e in entries:
                if e.directive_type.value == "no_op" and e.applies is False:
                    # Validator already downgraded; count by type as best-effort.
                    directive_validation_failures.labels(directive_type=e.directive_type.value).inc()
        return entries, used_llm, elapsed


__all__ = ["Orchestrator", "OrchestratorResult"]
