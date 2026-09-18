"""Deterministic validator for the LLM interpretation output.

Implements PRD section 08 (LLM interpretation guardrails) and section 11.1
(interpretation checks). On any violation we never crash — we map the
malformed entry to a safe `no_op` so the schedule stays valid.

This is the single source of truth used by both the live LLM path and the
regex fallback parser.
"""
from __future__ import annotations

import math
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from app.models import (
    DirectiveInterpretation,
    DirectiveType,
    HOURS_IN_DAY,
    HOUR_MAX,
    HOUR_MIN,
)


# ---------------------------------------------------------------------------
# Public entrypoints
# ---------------------------------------------------------------------------


def safe_validate_directives(
    raw_directives: list[dict[str, Any]],
    *,
    n_notes: int,
    battery_capacity_kwh: float | None = None,
) -> list[DirectiveInterpretation]:
    """Validate an LLM response (or fallback parser output) and produce safe DirectiveInterpretation entries.

    Rules:
      - Always returns exactly `n_notes` entries.
      - Entries are returned in note_index order 0..n_notes-1.
      - Malformed entries are downgraded to no_op with applies=false, structured_adjustment=null.
      - Extra or missing entries are tolerated by padding or truncating with safe defaults.
    """
    # Step 1: normalise to exactly n_notes dicts, padding/truncating safely.
    normalised: list[dict[str, Any] | None] = []
    for i in range(n_notes):
        if i < len(raw_directives):
            normalised.append(raw_directives[i])
        else:
            normalised.append(None)

    out: list[DirectiveInterpretation] = []
    for note_index, raw in enumerate(normalised):
        out.append(_safe_validate_single(raw, note_index=note_index, battery_capacity_kwh=battery_capacity_kwh))
    return out


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _safe_validate_single(
    raw: dict[str, Any] | None,
    *,
    note_index: int,
    battery_capacity_kwh: float | None,
) -> DirectiveInterpretation:
    """Validate one entry, falling back to no_op on any failure."""
    if raw is None or not isinstance(raw, dict):
        return _no_op(note_index, "missing or malformed entry from LLM")

    # Quick pre-screen: must contain the required keys.
    if not {"note_index", "applies", "directive_type", "explanation"}.issubset(raw):
        return _no_op(note_index, "missing required fields")

    # note_index must match the slot we expect.
    if raw.get("note_index") != note_index:
        return _no_op(note_index, f"note_index mismatch (got {raw.get('note_index')!r})")

    directive_type_raw = raw.get("directive_type")
    try:
        directive_type = DirectiveType(directive_type_raw)
    except ValueError:
        return _no_op(note_index, f"unsupported directive_type {directive_type_raw!r}")

    if directive_type is DirectiveType.NO_OP:
        if raw.get("applies") is not False:
            return _no_op(note_index, "no_op must have applies=false")
        if raw.get("structured_adjustment") is not None:
            return _no_op(note_index, "no_op must have structured_adjustment=null")
        try:
            return DirectiveInterpretation(
                note_index=note_index,
                applies=False,
                directive_type=DirectiveType.NO_OP,
                structured_adjustment=None,
                explanation=str(raw.get("explanation") or "no-op directive"),
            )
        except PydanticValidationError:
            return _no_op(note_index, "pydantic rejected no_op entry")

    # Non-no_op branch.
    if raw.get("applies") is not True:
        return _no_op(note_index, f"{directive_type.value} must have applies=true")

    sa = raw.get("structured_adjustment")
    if not isinstance(sa, dict):
        return _no_op(note_index, f"{directive_type.value} requires a structured_adjustment object")

    # Per-type validation.
    validation_error: str | None = None
    try:
        if directive_type is DirectiveType.SOLAR_REDUCTION:
            validated = _validate_solar_reduction(sa)
        elif directive_type is DirectiveType.MINIMUM_BATTERY_RESERVE:
            validated = _validate_minimum_battery_reserve(sa, battery_capacity_kwh)
        elif directive_type is DirectiveType.NO_CHARGE_WINDOW:
            validated = _validate_window(sa, required_keys={"hours"})
        elif directive_type is DirectiveType.NO_DISCHARGE_WINDOW:
            validated = _validate_window(sa, required_keys={"hours"})
        elif directive_type is DirectiveType.MAX_GRID_WINDOW:
            validated = _validate_max_grid(sa)
        else:  # pragma: no cover - unreachable
            validation_error = f"unknown directive_type {directive_type.value}"
            validated = None
    except _FieldError as e:
        validation_error = str(e)
        validated = None

    if validation_error is not None or validated is None:
        return _no_op(note_index, validation_error or "validation failed")

    try:
        return DirectiveInterpretation(
            note_index=note_index,
            applies=True,
            directive_type=directive_type,
            structured_adjustment=validated,
            explanation=str(raw.get("explanation") or f"{directive_type.value} applied"),
        )
    except PydanticValidationError as e:
        return _no_op(note_index, f"pydantic rejected entry: {e}")


def _validate_solar_reduction(sa: dict[str, Any]) -> dict[str, Any]:
    hours = _validate_hours(sa.get("hours"))
    factor = sa.get("factor")
    if not _is_finite_number(factor):
        raise _FieldError("factor must be a finite number")
    if not (0.0 <= factor <= 1.0):
        raise _FieldError("factor must be in [0, 1]")
    return {"hours": hours, "factor": float(factor)}


def _validate_minimum_battery_reserve(
    sa: dict[str, Any], battery_capacity_kwh: float | None
) -> dict[str, Any]:
    hours = _validate_hours(sa.get("hours"))
    minimum = sa.get("minimum_energy_kwh")
    if not _is_finite_number(minimum):
        raise _FieldError("minimum_energy_kwh must be a finite number")
    if minimum < 0:
        raise _FieldError("minimum_energy_kwh must be >= 0")
    if battery_capacity_kwh is not None and minimum > battery_capacity_kwh:
        raise _FieldError("minimum_energy_kwh must not exceed battery capacity")
    return {"hours": hours, "minimum_energy_kwh": float(minimum)}


def _validate_max_grid(sa: dict[str, Any]) -> dict[str, Any]:
    hours = _validate_hours(sa.get("hours"))
    cap = sa.get("max_grid_kwh")
    if not _is_finite_number(cap):
        raise _FieldError("max_grid_kwh must be a finite number")
    if cap < 0:
        raise _FieldError("max_grid_kwh must be >= 0")
    return {"hours": hours, "max_grid_kwh": float(cap)}


def _validate_window(sa: dict[str, Any], *, required_keys: set[str]) -> dict[str, Any]:
    extra = set(sa.keys()) - required_keys
    if extra:
        raise _FieldError(f"unexpected fields in window adjustment: {sorted(extra)}")
    hours = _validate_hours(sa.get("hours"))
    return {"hours": hours}


def _validate_hours(raw_hours: Any) -> list[int]:
    if not isinstance(raw_hours, list):
        raise _FieldError("hours must be a list")
    if not raw_hours:
        raise _FieldError("hours must be non-empty")
    if len(raw_hours) > HOURS_IN_DAY:
        raise _FieldError(f"hours must have at most {HOURS_IN_DAY} entries")
    ints: list[int] = []
    for h in raw_hours:
        if isinstance(h, bool) or not isinstance(h, int):
            raise _FieldError(f"hour {h!r} must be an integer")
        if not (HOUR_MIN <= h <= HOUR_MAX):
            raise _FieldError(f"hour {h} out of range [{HOUR_MIN}, {HOUR_MAX}]")
        ints.append(h)
    if len(set(ints)) != len(ints):
        raise _FieldError("hours must be unique")
    if ints != sorted(ints):
        raise _FieldError("hours must be in ascending order")
    return ints


def _is_finite_number(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return math.isfinite(float(v))
    return False


def _no_op(note_index: int, reason: str) -> DirectiveInterpretation:
    return DirectiveInterpretation(
        note_index=note_index,
        applies=False,
        directive_type=DirectiveType.NO_OP,
        structured_adjustment=None,
        explanation=f"no_op (validator fallback: {reason})",
    )


class _FieldError(Exception):
    """Internal validation failure marker."""
