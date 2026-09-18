"""Pydantic schemas for the GridWise LLM HTTP API.

Implements PRD sections 07 (request schema), 10 (response schema) and the
type constraints referenced in sections 04, 08, and 11.

All numeric fields are constrained with Field bounds so that bad input is
rejected at the edge of the service with HTTP 400, never reaching the LLM
or the optimizer.
"""
from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Constants from the PRD
# ---------------------------------------------------------------------------

HOURS_IN_DAY: int = 24
HOUR_MIN: int = 0
HOUR_MAX: int = 23
MIN_NOTES: int = 1
MAX_NOTES: int = 3

# Numeric tolerance from PRD §11.5 used by the replay checker.
TOLERANCE_KWH: float = 0.01
TOLERANCE_BDT: float = 0.01


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class DirectiveType(str, Enum):
    """The six supported directive types from PRD §04.1."""

    SOLAR_REDUCTION = "solar_reduction"
    MINIMUM_BATTERY_RESERVE = "minimum_battery_reserve"
    NO_CHARGE_WINDOW = "no_charge_window"
    NO_DISCHARGE_WINDOW = "no_discharge_window"
    MAX_GRID_WINDOW = "max_grid_window"
    NO_OP = "no_op"


class BatteryAction(str, Enum):
    """Exactly one action per hour (PRD §10.3)."""

    CHARGE = "charge"
    DISCHARGE = "discharge"
    IDLE = "idle"


# ---------------------------------------------------------------------------
# Request models (PRD §07)
# ---------------------------------------------------------------------------


class HourEntry(BaseModel):
    """One hour of the 24-hour scenario (PRD §07.2)."""

    model_config = ConfigDict(extra="forbid")

    hour: Annotated[int, Field(ge=HOUR_MIN, le=HOUR_MAX)]
    demand_kwh: Annotated[float, Field(ge=0.0, finite=True)]
    solar_kwh: Annotated[float, Field(ge=0.0, finite=True)]
    tariff_bdt_per_kwh: Annotated[float, Field(ge=0.0, finite=True)]


class BatterySpec(BaseModel):
    """Battery parameters (PRD §07.3)."""

    model_config = ConfigDict(extra="forbid")

    capacity_kwh: Annotated[float, Field(gt=0.0, finite=True)]
    initial_energy_kwh: Annotated[float, Field(ge=0.0, finite=True)]
    minimum_energy_kwh: Annotated[float, Field(ge=0.0, finite=True)]
    max_charge_kwh_per_hour: Annotated[float, Field(gt=0.0, finite=True)]
    max_discharge_kwh_per_hour: Annotated[float, Field(gt=0.0, finite=True)]

    @model_validator(mode="after")
    def _check_invariants(self) -> "BatterySpec":
        if self.initial_energy_kwh > self.capacity_kwh:
            raise ValueError(
                "initial_energy_kwh must not exceed capacity_kwh "
                f"(got {self.initial_energy_kwh} > {self.capacity_kwh})"
            )
        if self.minimum_energy_kwh > self.capacity_kwh:
            raise ValueError("minimum_energy_kwh must not exceed capacity_kwh")
        if self.minimum_energy_kwh > self.initial_energy_kwh:
            raise ValueError(
                "minimum_energy_kwh must not exceed initial_energy_kwh on day start"
            )
        return self


class OptimizeRequest(BaseModel):
    """Top-level POST /optimize-energy request body (PRD §07.1)."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: Annotated[str, Field(min_length=1, max_length=128)]
    operator_notes: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=2000)]],
        Field(min_length=MIN_NOTES, max_length=MAX_NOTES),
    ]
    hours: Annotated[list[HourEntry], Field(min_length=HOURS_IN_DAY, max_length=HOURS_IN_DAY)]
    battery: BatterySpec

    @field_validator("operator_notes")
    @classmethod
    def _strip_notes(cls, v: list[str]) -> list[str]:
        out = [n.strip() for n in v]
        if any(not n for n in out):
            raise ValueError("operator_notes must contain non-empty strings")
        return out

    @model_validator(mode="after")
    def _check_hours_unique(self) -> "OptimizeRequest":
        seen = sorted(h.hour for h in self.hours)
        expected = list(range(HOURS_IN_DAY))
        if seen != expected:
            raise ValueError(
                f"hours must be exactly {HOURS_IN_DAY} unique integers "
                f"{HOUR_MIN}..{HOUR_MAX}; got {seen}"
            )
        return self


# ---------------------------------------------------------------------------
# Response models (PRD §10)
# ---------------------------------------------------------------------------


# ---- Structured adjustment shapes (PRD §04.1) -----------------------------


class SolarReductionAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hours: list[int]
    factor: float


class MinimumBatteryReserveAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hours: list[int]
    minimum_energy_kwh: float


class NoChargeWindowAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hours: list[int]


class NoDischargeWindowAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hours: list[int]


class MaxGridWindowAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hours: list[int]
    max_grid_kwh: float


# A permissive alias for the union of possible structured adjustments.
StructuredAdjustment = (
    SolarReductionAdjustment
    | MinimumBatteryReserveAdjustment
    | NoChargeWindowAdjustment
    | NoDischargeWindowAdjustment
    | MaxGridWindowAdjustment
)


class DirectiveInterpretation(BaseModel):
    """One entry in directive_interpretation[] (PRD §10.2)."""

    model_config = ConfigDict(extra="forbid")

    note_index: Annotated[int, Field(ge=0)]
    applies: bool
    directive_type: DirectiveType
    structured_adjustment: dict[str, Any] | None = None
    explanation: Annotated[str, Field(min_length=0, max_length=500)]

    @model_validator(mode="after")
    def _check_no_op_shape(self) -> "DirectiveInterpretation":
        if self.directive_type is DirectiveType.NO_OP:
            # PRD §08: applies = false AND structured_adjustment = null
            if self.applies is not False:
                raise ValueError("no_op must have applies=false")
            if self.structured_adjustment is not None:
                raise ValueError("no_op must have structured_adjustment=null")
        else:
            if self.applies is not True:
                raise ValueError(f"{self.directive_type} must have applies=true")
            if self.structured_adjustment is None:
                raise ValueError(
                    f"{self.directive_type} must have a structured_adjustment"
                )
        return self


class HourlyPlanEntry(BaseModel):
    """One hour of the produced schedule (PRD §10.3)."""

    model_config = ConfigDict(extra="forbid")

    hour: Annotated[int, Field(ge=HOUR_MIN, le=HOUR_MAX)]
    grid_kwh: Annotated[float, Field(ge=0.0, finite=True)]
    solar_used_kwh: Annotated[float, Field(ge=0.0, finite=True)]
    battery_action: BatteryAction
    battery_kwh: Annotated[float, Field(ge=0.0, finite=True)]
    battery_energy_after_kwh: Annotated[float, Field(ge=0.0, finite=True)]

    @model_validator(mode="after")
    def _check_action_consistency(self) -> "HourlyPlanEntry":
        if self.battery_action is BatteryAction.IDLE and self.battery_kwh != 0.0:
            raise ValueError("battery_kwh must be 0 when battery_action=idle")
        if self.battery_action is not BatteryAction.IDLE and self.battery_kwh <= 0.0:
            raise ValueError(
                "battery_kwh must be positive when battery_action is charge/discharge"
            )
        return self


class OptimizeResponse(BaseModel):
    """Top-level POST /optimize-energy response (PRD §10.1)."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: Annotated[str, Field(min_length=1, max_length=128)]
    directive_interpretation: list[DirectiveInterpretation]
    hourly_plan: Annotated[
        list[HourlyPlanEntry], Field(min_length=HOURS_IN_DAY, max_length=HOURS_IN_DAY)
    ]
    total_grid_kwh: Annotated[float, Field(ge=0.0, finite=True)]
    total_cost_bdt: Annotated[float, Field(ge=0.0, finite=True)]
    peak_grid_kwh: Annotated[float, Field(ge=0.0, finite=True)]
    plan_summary: Annotated[str, Field(min_length=0, max_length=2000)]


class HealthResponse(BaseModel):
    """GET /health response body (PRD §06.2)."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"


# ---- LLM raw payload type -------------------------------------------------


# Raw dict shape returned by the LLM before we validate it into DirectiveInterpretation.
RawDirectiveDict = dict[str, Any]
