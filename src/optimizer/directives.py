"""Apply validated directive interpretations to the raw hour data.

Builds the `effective_*` arrays the optimizer and the replay checker both
consume. The math here mirrors PRD §5.3 exactly and is the single source
of truth used by both code paths.

Inputs are validated `DirectiveInterpretation` entries — anything invalid
was already downgraded to no_op by the validator.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.models import DirectiveInterpretation, DirectiveType, HourEntry


@dataclass(frozen=True)
class EffectiveArrays:
    """Per-hour arrays the LP consumes.

    All arrays have exactly 24 entries indexed 0..23.
    """

    effective_solar: list[float]
    effective_min_reserve: list[float]
    charge_allowed: list[bool]
    discharge_allowed: list[bool]
    max_grid: list[float]


def build_effective_arrays(
    hours: list[HourEntry],
    interpretations: list[DirectiveInterpretation],
    *,
    battery_capacity_kwh: float,
    base_min_reserve_kwh: float,
) -> EffectiveArrays:
    """Apply §5.3 directive effects to the raw hour data."""
    if len(hours) != 24:
        raise ValueError("build_effective_arrays requires exactly 24 hour entries")

    effective_solar = [float(h.solar_kwh) for h in hours]
    effective_min_reserve = [float(base_min_reserve_kwh) for _ in range(24)]
    charge_allowed = [True] * 24
    discharge_allowed = [True] * 24
    # PRD §5.3: max_grid defaults to +inf unless a directive sets a cap.
    max_grid = [float("inf")] * 24

    for entry in interpretations:
        if not entry.applies or entry.directive_type is DirectiveType.NO_OP:
            continue
        sa = entry.structured_adjustment
        if sa is None:  # defensive: validator guarantees this won't happen
            continue
        hours_idx: list[int] = list(sa.get("hours", []))
        if entry.directive_type is DirectiveType.SOLAR_REDUCTION:
            factor = float(sa["factor"])
            for h in hours_idx:
                effective_solar[h] = effective_solar[h] * factor
        elif entry.directive_type is DirectiveType.MINIMUM_BATTERY_RESERVE:
            target = float(sa["minimum_energy_kwh"])
            for h in hours_idx:
                if target > effective_min_reserve[h]:
                    effective_min_reserve[h] = target
            # Also clip to capacity (defensive — validator already enforced this).
            for h in hours_idx:
                if effective_min_reserve[h] > battery_capacity_kwh:
                    effective_min_reserve[h] = battery_capacity_kwh
        elif entry.directive_type is DirectiveType.NO_CHARGE_WINDOW:
            for h in hours_idx:
                charge_allowed[h] = False
        elif entry.directive_type is DirectiveType.NO_DISCHARGE_WINDOW:
            for h in hours_idx:
                discharge_allowed[h] = False
        elif entry.directive_type is DirectiveType.MAX_GRID_WINDOW:
            cap = float(sa["max_grid_kwh"])
            for h in hours_idx:
                if cap < max_grid[h]:
                    max_grid[h] = cap

    return EffectiveArrays(
        effective_solar=effective_solar,
        effective_min_reserve=effective_min_reserve,
        charge_allowed=charge_allowed,
        discharge_allowed=discharge_allowed,
        max_grid=max_grid,
    )
