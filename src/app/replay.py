"""Post-optimization replay checker.

Re-runs every consistency check from PRD §11.3 against the produced
schedule. If any check fails we raise `ReplayError` so the FastAPI
handler can return HTTP 500 — this catches silent model drift between
the LP and the judge.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from app.models import (
    BatteryAction,
    DirectiveInterpretation,
    DirectiveType,
    HOURS_IN_DAY,
    HourEntry,
    HourlyPlanEntry,
    OptimizeRequest,
    OptimizeResponse,
    TOLERANCE_BDT,
    TOLERANCE_KWH,
)
from optimizer.directives import EffectiveArrays, build_effective_arrays


class ReplayError(Exception):
    """Raised when the produced schedule violates a PRD §11.3 rule."""


@dataclass(frozen=True)
class ReplayOutcome:
    ok: bool
    errors: tuple[str, ...] = ()


def replay_check(request: OptimizeRequest, response: OptimizeResponse) -> ReplayOutcome:
    """Run every §11.3 check and return the cumulative outcome."""
    errors: list[str] = []

    # 1. scenario_id matches.
    if request.scenario_id != response.scenario_id:
        errors.append(
            f"scenario_id mismatch (req={request.scenario_id!r}, resp={response.scenario_id!r})"
        )

    # 2. Hours array structure.
    hours = request.hours
    if len(hours) != HOURS_IN_DAY:
        errors.append(f"hours length {len(hours)} != {HOURS_IN_DAY}")

    # 3. directive_interpretation length matches operator_notes.
    if len(response.directive_interpretation) != len(request.operator_notes):
        errors.append(
            "directive_interpretation length "
            f"{len(response.directive_interpretation)} != "
            f"operator_notes length {len(request.operator_notes)}"
        )

    # 4. hourly_plan has exactly 24 unique hours 0..23.
    plan_hours = sorted(p.hour for p in response.hourly_plan)
    if plan_hours != list(range(HOURS_IN_DAY)):
        errors.append(f"hourly_plan hours {plan_hours} != 0..23")

    # 5. Required numerics finite/non-negative.
    for p in response.hourly_plan:
        for name, value in (
            ("grid_kwh", p.grid_kwh),
            ("solar_used_kwh", p.solar_used_kwh),
            ("battery_kwh", p.battery_kwh),
            ("battery_energy_after_kwh", p.battery_energy_after_kwh),
        ):
            if math.isnan(value) or math.isinf(value):
                errors.append(f"hour {p.hour}: {name} is not finite ({value})")
            if value < -TOLERANCE_KWH:
                errors.append(f"hour {p.hour}: {name} is negative ({value})")

    # 6. battery_action consistency.
    for p in response.hourly_plan:
        if p.battery_action is BatteryAction.IDLE and p.battery_kwh > TOLERANCE_KWH:
            errors.append(
                f"hour {p.hour}: battery_action=idle but battery_kwh={p.battery_kwh}"
            )
        if p.battery_action is not BatteryAction.IDLE and p.battery_kwh <= TOLERANCE_KWH:
            errors.append(
                f"hour {p.hour}: battery_action={p.battery_action.value} but battery_kwh={p.battery_kwh}"
            )

    # 7. Build effective arrays and validate every directive.
    try:
        effective = build_effective_arrays(
            hours,
            response.directive_interpretation,
            battery_capacity_kwh=request.battery.capacity_kwh,
            base_min_reserve_kwh=request.battery.minimum_energy_kwh,
        )
    except Exception as e:  # pragma: no cover - defensive
        errors.append(f"failed to build effective arrays: {e}")
        return ReplayOutcome(ok=False, errors=tuple(errors))

    demand_by_hour = {h.hour: float(h.demand_kwh) for h in hours}

    # 8. Walk the schedule and check every transition / bound.
    prev_energy = request.battery.initial_energy_kwh
    for p in response.hourly_plan:
        h = p.hour
        expected_min = effective.effective_min_reserve[h]
        cap = request.battery.capacity_kwh

        # Battery bounds.
        if p.battery_energy_after_kwh + TOLERANCE_KWH < expected_min:
            errors.append(
                f"hour {h}: battery_energy_after_kwh={p.battery_energy_after_kwh} "
                f"below effective min {expected_min}"
            )
        if p.battery_energy_after_kwh > cap + TOLERANCE_KWH:
            errors.append(
                f"hour {h}: battery_energy_after_kwh={p.battery_energy_after_kwh} "
                f"above capacity {cap}"
            )

        # Battery transition.
        if p.battery_action is BatteryAction.CHARGE:
            delta = +p.battery_kwh
        elif p.battery_action is BatteryAction.DISCHARGE:
            delta = -p.battery_kwh
        else:
            delta = 0.0
        expected_after = prev_energy + delta
        if abs(expected_after - p.battery_energy_after_kwh) > TOLERANCE_KWH:
            errors.append(
                f"hour {h}: battery transition mismatch "
                f"(prev={prev_energy}, delta={delta}, "
                f"expected={expected_after}, actual={p.battery_energy_after_kwh})"
            )

        # Hourly rate limits.
        if p.battery_action is BatteryAction.CHARGE:
            if p.battery_kwh > request.battery.max_charge_kwh_per_hour + TOLERANCE_KWH:
                errors.append(
                    f"hour {h}: charge {p.battery_kwh} exceeds max_charge "
                    f"{request.battery.max_charge_kwh_per_hour}"
                )
            if not effective.charge_allowed[h]:
                errors.append(f"hour {h}: charge not allowed by directive")
        if p.battery_action is BatteryAction.DISCHARGE:
            if p.battery_kwh > request.battery.max_discharge_kwh_per_hour + TOLERANCE_KWH:
                errors.append(
                    f"hour {h}: discharge {p.battery_kwh} exceeds max_discharge "
                    f"{request.battery.max_discharge_kwh_per_hour}"
                )
            if not effective.discharge_allowed[h]:
                errors.append(f"hour {h}: discharge not allowed by directive")

        # Solar usage within effective.
        if p.solar_used_kwh > effective.effective_solar[h] + TOLERANCE_KWH:
            errors.append(
                f"hour {h}: solar_used {p.solar_used_kwh} exceeds effective solar "
                f"{effective.effective_solar[h]}"
            )

        # Energy balance (PRD §9.5).
        d = demand_by_hour[h]
        balance_lhs = p.grid_kwh + p.solar_used_kwh + (
            p.battery_kwh if p.battery_action is BatteryAction.DISCHARGE else 0.0
        )
        balance_rhs = d + (
            p.battery_kwh if p.battery_action is BatteryAction.CHARGE else 0.0
        )
        if abs(balance_lhs - balance_rhs) > TOLERANCE_KWH:
            errors.append(
                f"hour {h}: energy balance mismatch "
                f"(lhs={balance_lhs}, rhs={balance_rhs}, demand={d})"
            )

        # Operator max_grid cap.
        if math.isfinite(effective.max_grid[h]):
            if p.grid_kwh > effective.max_grid[h] + TOLERANCE_KWH:
                errors.append(
                    f"hour {h}: grid_kwh {p.grid_kwh} exceeds max_grid "
                    f"{effective.max_grid[h]}"
                )

        prev_energy = p.battery_energy_after_kwh

    # 9. End-of-day neutrality (PRD §9.6).
    final_energy = response.hourly_plan[-1].battery_energy_after_kwh
    if abs(final_energy - request.battery.initial_energy_kwh) > TOLERANCE_KWH:
        errors.append(
            "end-of-day battery mismatch: "
            f"final={final_energy} initial={request.battery.initial_energy_kwh}"
        )

    # 10. Totals match.
    recomputed_total_grid = sum(p.grid_kwh for p in response.hourly_plan)
    if abs(recomputed_total_grid - response.total_grid_kwh) > TOLERANCE_KWH:
        errors.append(
            f"total_grid_kwh mismatch (replayed={recomputed_total_grid}, "
            f"reported={response.total_grid_kwh})"
        )
    tariff_by_hour = {h.hour: float(h.tariff_bdt_per_kwh) for h in hours}
    recomputed_total_cost = sum(
        p.grid_kwh * tariff_by_hour[p.hour] for p in response.hourly_plan
    )
    if abs(recomputed_total_cost - response.total_cost_bdt) > TOLERANCE_BDT:
        errors.append(
            f"total_cost_bdt mismatch (replayed={recomputed_total_cost}, "
            f"reported={response.total_cost_bdt})"
        )
    recomputed_peak = max((p.grid_kwh for p in response.hourly_plan), default=0.0)
    if abs(recomputed_peak - response.peak_grid_kwh) > TOLERANCE_KWH:
        errors.append(
            f"peak_grid_kwh mismatch (replayed={recomputed_peak}, "
            f"reported={response.peak_grid_kwh})"
        )

    return ReplayOutcome(ok=not errors, errors=tuple(errors))


def assert_replay(request: OptimizeRequest, response: OptimizeResponse) -> None:
    """Raise ReplayError if any check fails."""
    outcome = replay_check(request, response)
    if not outcome.ok:
        raise ReplayError("; ".join(outcome.errors))


__all__ = ["ReplayError", "ReplayOutcome", "replay_check", "assert_replay"]
