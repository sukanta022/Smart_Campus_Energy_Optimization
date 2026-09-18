"""PuLP-based 24-hour energy scheduling LP.

Implements the optimization problem defined by PRD sections 5.2, 5.3, and 9.
The objective is `minimize Σ grid_kwh[h] * tariff_bdt_per_kwh[h]`.

Variables (per hour h in 0..23):
  - grid[h]        >= 0
  - solar_used[h]  in [0, effective_solar[h]]
  - charge[h]      in [0, charge_allowed[h] * max_charge_per_hour]
  - discharge[h]   in [0, discharge_allowed[h] * max_discharge_per_hour]
  - E_after[h]     in [effective_min_reserve[h], battery_capacity_kwh]

Constraints:
  - Battery transition: E_after[h] = E_before[h] + charge[h] - discharge[h]
                        where E_before[0] = initial_energy_kwh
                        and E_before[h+1] = E_after[h]
  - Hourly rate limits: charge[h] <= max_charge_kwh_per_hour, etc.
  - Energy balance (PRD §9.5):
        grid[h] + solar_used[h] + discharge[h] = demand[h] + charge[h]
  - End-of-day neutrality (PRD §9.6): E_after[23] = initial_energy_kwh
  - Operator directives: grid[h] <= max_grid[h] when capped.

A warm-start from a greedy tariff-arbitrage heuristic primes PuLP for
fast first-feasible solutions.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Iterable

import pulp

from app.models import BatteryAction, HourEntry, HourlyPlanEntry, OptimizeResponse
from optimizer.directives import EffectiveArrays

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScheduleResult:
    plan: list[HourlyPlanEntry]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    status: str  # "optimal" | "infeasible" | "unbounded" | ...


class OptimizationError(Exception):
    """Raised when PuLP cannot find a feasible / optimal solution."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def optimize_schedule(
    hours: list[HourEntry],
    *,
    initial_energy_kwh: float,
    capacity_kwh: float,
    max_charge_kwh_per_hour: float,
    max_discharge_kwh_per_hour: float,
    effective: EffectiveArrays,
    warm_start: bool = True,
    time_limit_seconds: float = 3.0,
) -> ScheduleResult:
    """Solve the 24-hour LP and return the optimal (or best) schedule."""
    if len(hours) != 24:
        raise ValueError("optimize_schedule requires exactly 24 hour entries")

    demand = [float(h.demand_kwh) for h in hours]
    tariff = [float(h.tariff_bdt_per_kwh) for h in hours]

    # ---- Build the LP -----------------------------------------------------
    prob = pulp.LpProblem("gridwise_24h", pulp.LpMinimize)
    H = list(range(24))

    grid = [pulp.LpVariable(f"grid_{h}", lowBound=0.0) for h in H]
    solar_used = [
        pulp.LpVariable(f"solar_{h}", lowBound=0.0, upBound=effective.effective_solar[h])
        for h in H
    ]
    charge = [
        pulp.LpVariable(
            f"charge_{h}",
            lowBound=0.0,
            upBound=max_charge_kwh_per_hour if effective.charge_allowed[h] else 0.0,
        )
        for h in H
    ]
    discharge = [
        pulp.LpVariable(
            f"discharge_{h}",
            lowBound=0.0,
            upBound=max_discharge_kwh_per_hour if effective.discharge_allowed[h] else 0.0,
        )
        for h in H
    ]
    e_after = [
        pulp.LpVariable(
            f"e_after_{h}",
            lowBound=effective.effective_min_reserve[h],
            upBound=capacity_kwh,
        )
        for h in H
    ]

    # ---- Objective -------------------------------------------------------
    prob += pulp.lpSum(grid[h] * tariff[h] for h in H), "total_grid_cost"

    # ---- Battery transition ----------------------------------------------
    for h in H:
        e_before = initial_energy_kwh if h == 0 else e_after[h - 1]
        prob += e_after[h] == e_before + charge[h] - discharge[h], f"battery_transition_{h}"

    # ---- Energy balance (PRD §9.5) ---------------------------------------
    for h in H:
        prob += grid[h] + solar_used[h] + discharge[h] == demand[h] + charge[h], f"balance_{h}"

    # ---- End-of-day neutrality (PRD §9.6) -------------------------------
    prob += e_after[23] == initial_energy_kwh, "end_of_day_neutrality"

    # ---- Operator grid caps (PRD §5.3 max_grid_window) -------------------
    for h in H:
        if math.isfinite(effective.max_grid[h]):
            prob += grid[h] <= effective.max_grid[h], f"max_grid_{h}"

    # ---- Warm-start heuristic --------------------------------------------
    if warm_start:
        warm = _greedy_warm_start(
            demand=demand,
            tariff=tariff,
            initial_energy_kwh=initial_energy_kwh,
            capacity_kwh=capacity_kwh,
            max_charge_kwh_per_hour=max_charge_kwh_per_hour,
            max_discharge_kwh_per_hour=max_discharge_kwh_per_hour,
            effective=effective,
        )
        for h in H:
            grid[h].setInitialValue(warm["grid"][h])
            solar_used[h].setInitialValue(warm["solar_used"][h])
            charge[h].setInitialValue(warm["charge"][h])
            discharge[h].setInitialValue(warm["discharge"][h])
            e_after[h].setInitialValue(warm["e_after"][h])

    # ---- Solve -----------------------------------------------------------
    solver = pulp.PULP_CBC_CMD(msg=False, timeLimit=time_limit_seconds)
    status = prob.solve(solver)
    status_name = pulp.LpStatus[status].lower()

    if status_name not in ("optimal", "feasible"):
        raise OptimizationError(f"optimizer returned status: {status_name}")

    # ---- Extract --------------------------------------------------------
    plan: list[HourlyPlanEntry] = []
    total_grid = 0.0
    total_cost = 0.0
    peak = 0.0
    for h in H:
        g = max(0.0, float(pulp.value(grid[h]) or 0.0))
        s = max(0.0, float(pulp.value(solar_used[h]) or 0.0))
        c = max(0.0, float(pulp.value(charge[h]) or 0.0))
        d = max(0.0, float(pulp.value(discharge[h]) or 0.0))
        ea = float(pulp.value(e_after[h]) or initial_energy_kwh)
        # Round tiny numeric noise below tolerance to zero.
        if g < 1e-6:
            g = 0.0
        if s < 1e-6:
            s = 0.0
        if c < 1e-6:
            c = 0.0
        if d < 1e-6:
            d = 0.0
        if abs(ea - round(ea, 6)) < 1e-6:
            ea = round(ea, 6)
        # Decide action.
        if c > d + 1e-6 and c > 0:
            action = BatteryAction.CHARGE
            magnitude = c
        elif d > c + 1e-6 and d > 0:
            action = BatteryAction.DISCHARGE
            magnitude = d
        else:
            action = BatteryAction.IDLE
            magnitude = 0.0
        plan.append(
            HourlyPlanEntry(
                hour=h,
                grid_kwh=g,
                solar_used_kwh=s,
                battery_action=action,
                battery_kwh=magnitude,
                battery_energy_after_kwh=ea,
            )
        )
        total_grid += g
        total_cost += g * tariff[h]
        peak = max(peak, g)

    return ScheduleResult(
        plan=plan,
        total_grid_kwh=total_grid,
        total_cost_bdt=total_cost,
        peak_grid_kwh=peak,
        status=status_name,
    )


# ---------------------------------------------------------------------------
# Greedy warm-start
# ---------------------------------------------------------------------------


def _greedy_warm_start(
    *,
    demand: list[float],
    tariff: list[float],
    initial_energy_kwh: float,
    capacity_kwh: float,
    max_charge_kwh_per_hour: float,
    max_discharge_kwh_per_hour: float,
    effective: EffectiveArrays,
) -> dict[str, list[float]]:
    """Cheap-feasible greedy heuristic that primes PuLP's simplex start."""
    n = 24
    grid = [0.0] * n
    solar_used = [0.0] * n
    charge = [0.0] * n
    discharge = [0.0] * n
    e_after = [0.0] * n

    e = initial_energy_kwh
    for h in range(n):
        # Use as much solar as possible (free, so always optimal).
        s = min(effective.effective_solar[h], demand[h])
        solar_used[h] = s
        remaining = max(0.0, demand[h] - s)

        if remaining > 1e-9:
            # Discharge battery to cover remaining demand (if allowed and useful).
            if effective.discharge_allowed[h] and e > effective.effective_min_reserve[h]:
                headroom = e - effective.effective_min_reserve[h]
                d = min(max_discharge_kwh_per_hour, headroom, remaining)
                discharge[h] = d
                remaining -= d
                e -= d
            # Cover the rest from grid.
            if not math.isfinite(effective.max_grid[h]):
                grid[h] = remaining
            else:
                grid[h] = min(remaining, effective.max_grid[h])
        # Charge battery from any leftover solar (excess solar -> battery).
        if effective.charge_allowed[h]:
            solar_surplus = effective.effective_solar[h] - solar_used[h]
            room = capacity_kwh - e
            if solar_surplus > 1e-9 and room > 1e-9:
                c = min(max_charge_kwh_per_hour, solar_surplus, room)
                # Charging from solar doesn't change grid; but it must remain
                # consistent with energy balance: grid + solar_used + discharge
                # = demand + charge. If we increase charge we must increase
                # grid or solar_used. Use solar_used (curtailment is allowed).
                solar_used[h] = min(effective.effective_solar[h], solar_used[h] + c)
                charge[h] = c
                e += c
        e_after[h] = e

    # Enforce end-of-day neutrality in the warm-start by scaling battery action.
    delta = e - initial_energy_kwh
    if abs(delta) > 1e-6:
        # Find cheapest hour to charge (-delta) or discharge (+delta) and adjust.
        target_delta = -delta  # if delta > 0 we over-stored, need to discharge
        if target_delta > 0:
            # discharge in cheapest hour
            idx = min(range(n), key=lambda h: tariff[h])
            d = min(max_discharge_kwh_per_hour, target_delta, e_after[idx] - effective.effective_min_reserve[idx])
            discharge[idx] += d
            # adjust downstream state
            for h in range(idx, n):
                e_after[h] -= d
        else:
            need = -target_delta
            idx = max(range(n), key=lambda h: tariff[h])
            c = min(max_charge_kwh_per_hour, need, capacity_kwh - e_after[idx])
            charge[idx] += c
            for h in range(idx, n):
                e_after[h] += c

    return {
        "grid": grid,
        "solar_used": solar_used,
        "charge": charge,
        "discharge": discharge,
        "e_after": e_after,
    }


# ---------------------------------------------------------------------------
# Plan-summary text (LLM-cosmetic, falls back to a deterministic string)
# ---------------------------------------------------------------------------


def build_plan_summary(
    *,
    scenario_id: str,
    interpretations: Iterable,
    total_cost_bdt: float,
    total_grid_kwh: float,
    used_llm: bool,
) -> str:
    """Produce a deterministic, non-cosmetic plan summary.

    Per the PRD the LLM is required only on the *interpretation* path; the
    plan_summary field can be any short explanation. We compute it from
    deterministic facts so the service has zero LLM dependency here.
    """
    directives = [d for d in interpretations if getattr(d, "applies", False) and getattr(d, "directive_type", None) is not None]
    applied = sum(1 for d in directives if getattr(d, "directive_type", None) and getattr(d, "directive_type").value != "no_op")
    src = "LLM" if used_llm else "fallback parser"
    return (
        f"Scenario {scenario_id}: interpreted via {src}; applied {applied} directive(s); "
        f"total grid {total_grid_kwh:.2f} kWh, cost {total_cost_bdt:.2f} BDT."
    )


# ---------------------------------------------------------------------------
# Compose the OptimizeResponse from solver output
# ---------------------------------------------------------------------------


def compose_response(
    *,
    scenario_id: str,
    interpretations: list,
    result: ScheduleResult,
    used_llm: bool,
) -> OptimizeResponse:
    summary = build_plan_summary(
        scenario_id=scenario_id,
        interpretations=interpretations,
        total_cost_bdt=result.total_cost_bdt,
        total_grid_kwh=result.total_grid_kwh,
        used_llm=used_llm,
    )
    return OptimizeResponse(
        scenario_id=scenario_id,
        directive_interpretation=list(interpretations),
        hourly_plan=result.plan,
        total_grid_kwh=round(result.total_grid_kwh, 6),
        total_cost_bdt=round(result.total_cost_bdt, 6),
        peak_grid_kwh=round(result.peak_grid_kwh, 6),
        plan_summary=summary,
    )
