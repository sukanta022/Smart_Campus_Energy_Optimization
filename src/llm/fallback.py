"""Regex-based operator-note parser used as a fallback when the LLM is unavailable.

The PRD requires an LLM on the primary path, but explicitly allows the
service to fall back to a deterministic parser when the LLM fails — as
long as the interpretation still respects the §04 directive vocabulary
and §08 guardrails (we re-validate through `validator.safe_validate_directives`).
"""
from __future__ import annotations

import re
from typing import Any


def regex_parse(notes: list[str]) -> list[dict[str, Any]]:
    """Return a list of `n_notes` raw directive dicts (one per note)."""
    return [regex_parse_one(i, n) for i, n in enumerate(notes)]


# ---- per-note parsing ------------------------------------------------------

_NUMBER_WORDS = {
    "zero": 0.0, "one": 1.0, "two": 2.0, "three": 3.0, "four": 4.0, "five": 5.0,
    "six": 6.0, "seven": 7.0, "eight": 8.0, "nine": 9.0, "ten": 10.0,
    "eleven": 11.0, "twelve": 12.0,
}


def regex_parse_one(note_index: int, text: str) -> dict[str, Any]:
    text_lower = text.lower().strip()
    if not text_lower:
        return _no_op(note_index, "empty note")

    # Order matters: solar first (uses % patterns), then reserve, then charge/discharge windows, then max grid.
    parsed = (
        _try_solar_reduction(note_index, text_lower)
        or _try_minimum_reserve(note_index, text_lower)
        or _try_no_charge(note_index, text_lower)
        or _try_no_discharge(note_index, text_lower)
        or _try_max_grid(note_index, text_lower)
    )
    if parsed is not None:
        return parsed
    return _no_op(note_index, "no directive keywords matched")


def _no_op(note_index: int, reason: str) -> dict[str, Any]:
    return {
        "note_index": note_index,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": f"fallback no_op: {reason}",
    }


# ---- solar_reduction ------------------------------------------------------


_SOLAR_RE_KEYWORDS = re.compile(r"\b(solar|pv|panel|photovoltaic|rooftop)\b", re.I)
_REDUCTION_KEYWORDS = re.compile(
    r"\b(reduc(?:e|tion|tion of|ed)|drop|shortfall|shortage|wash(?:ing)?|maintenance|outage|lower)\b", re.I
)
_PERCENT_REMAIN_RE = re.compile(
    r"(?:drop|reduc(?:e|tion)|remain(?:ing)?|left|down)\s*(?:to|at|of|about|approximately|roughly|~)?\s*"
    r"(?P<percent>\d+(?:\.\d+)?)\s*(?:%|percent)\b",
    re.I,
)
_FRACTION_REMAIN_RE = re.compile(
    r"(?:one[-\s]+(?P<ord>first|second|third|fourth|fifth))\s*of\s*(?:normal|usual|typical|standard)\s*(?:solar|output|production|generation)",
    re.I,
)
_PERCENT_LOST_RE = re.compile(
    r"(?P<percent>\d+(?:\.\d+)?)\s*(?:%|percent)\s*(?:reduc(?:e|tion)|drop|shortfall|loss|less|lower)",
    re.I,
)


def _try_solar_reduction(note_index: int, text: str) -> dict[str, Any] | None:
    if not _SOLAR_RE_KEYWORDS.search(text) or not _REDUCTION_KEYWORDS.search(text):
        return None

    hours = _parse_hour_window(text)
    if hours is None:
        return None

    factor = _parse_reduction_factor(text)
    if factor is None:
        return None

    return {
        "note_index": note_index,
        "applies": True,
        "directive_type": "solar_reduction",
        "structured_adjustment": {"hours": hours, "factor": factor},
        "explanation": f"fallback: solar reduced to {factor} in {hours}",
    }


def _parse_reduction_factor(text: str) -> float | None:
    m = _PERCENT_REMAIN_RE.search(text)
    if m:
        return float(m.group("percent")) / 100.0
    m = _PERCENT_LOST_RE.search(text)
    if m:
        return max(0.0, 1.0 - float(m.group("percent")) / 100.0)
    m = _FRACTION_REMAIN_RE.search(text)
    if m:
        ord_map = {"first": 1.0, "second": 0.5, "third": 1.0 / 3.0, "fourth": 0.25, "fifth": 0.2}
        return ord_map.get(m.group("ord").lower())
    # "20% of normal" / "20 percent of normal"
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent)\s*of\s*(?:normal|usual|typical|standard)", text)
    if m:
        return float(m.group(1)) / 100.0
    return None


# ---- minimum_battery_reserve ----------------------------------------------


_RESERVE_KEYWORDS = re.compile(
    r"\b(reserve|reserved|reserve level|at least|minimum|keep|maintain|hold)\b", re.I
)
_RESERVE_KWH_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?:kwh|kWh|kWh|kw\s*h)", re.I)


def _try_minimum_reserve(note_index: int, text: str) -> dict[str, Any] | None:
    if "battery" not in text and "kwh" not in text.lower() and "kw h" not in text.lower():
        return None
    if not _RESERVE_KEYWORDS.search(text):
        return None
    hours = _parse_hour_window(text)
    if hours is None:
        return None

    m = _RESERVE_KWH_RE.search(text)
    if m:
        value = float(m.group("value"))
    else:
        # fall back to first bare number in the note
        m = re.search(r"\b(\d+(?:\.\d+)?)\b", text)
        if not m:
            return None
        value = float(m.group(1))
    return {
        "note_index": note_index,
        "applies": True,
        "directive_type": "minimum_battery_reserve",
        "structured_adjustment": {"hours": hours, "minimum_energy_kwh": value},
        "explanation": f"fallback: reserve >= {value} kWh in {hours}",
    }


# ---- charge/discharge windows ---------------------------------------------


def _try_no_charge(note_index: int, text: str) -> dict[str, Any] | None:
    if "charge" not in text:
        return None
    if not re.search(r"\b(no|do not|don'?t|cannot|can'?t|forbid|prohibit|prevent|avoid)\b", text):
        return None
    hours = _parse_hour_window(text)
    if hours is None:
        return None
    return {
        "note_index": note_index,
        "applies": True,
        "directive_type": "no_charge_window",
        "structured_adjustment": {"hours": hours},
        "explanation": f"fallback: no charge in {hours}",
    }


def _try_no_discharge(note_index: int, text: str) -> dict[str, Any] | None:
    if "discharge" not in text:
        return None
    if not re.search(r"\b(no|do not|don'?t|cannot|can'?t|forbid|prohibit|prevent|avoid)\b", text):
        return None
    hours = _parse_hour_window(text)
    if hours is None:
        return None
    return {
        "note_index": note_index,
        "applies": True,
        "directive_type": "no_discharge_window",
        "structured_adjustment": {"hours": hours},
        "explanation": f"fallback: no discharge in {hours}",
    }


# ---- max_grid_window -------------------------------------------------------


_MAX_GRID_KEYWORDS = re.compile(
    r"\b(max(?:imum)?\s+grid|grid\s+(?:cap|limit|max)|cap\s+grid|import\s+no\s+more\s+than)\b", re.I
)
_CAP_KWH_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?:kwh|kWh)", re.I)


def _try_max_grid(note_index: int, text: str) -> dict[str, Any] | None:
    if not _MAX_GRID_KEYWORDS.search(text):
        return None
    hours = _parse_hour_window(text)
    if hours is None:
        return None
    m = _CAP_KWH_RE.search(text)
    if not m:
        return None
    cap = float(m.group("value"))
    return {
        "note_index": note_index,
        "applies": True,
        "directive_type": "max_grid_window",
        "structured_adjustment": {"hours": hours, "max_grid_kwh": cap},
        "explanation": f"fallback: grid <= {cap} kWh in {hours}",
    }


# ---- hour window parser ---------------------------------------------------


_HOUR_WINDOW_RES: tuple[re.Pattern[str], ...] = (
    # "1 PM to 3 PM" / "1pm-3pm" / "between 1 PM and 3 PM" / "from 13:00 to 15:00"
    re.compile(
        r"(?:from|between)?\s*"
        r"(?P<a>\d{1,2})(?::\d{2})?\s*(?P<ap_a>a\.?m\.?|p\.?m\.?)?"
        r"\s*(?:to|until|till|-|and|\u2013|\u2014)\s*"
        r"(?P<b>\d{1,2})(?::\d{2})?\s*(?P<ap_b>a\.?m\.?|p\.?m\.?)?",
        re.I,
    ),
    # "1-3 PM" (suffix-applied form)
    re.compile(
        r"(?P<a>\d{1,2})\s*-\s*(?P<b>\d{1,2})\s*(?P<ap>a\.?m\.?|p\.?m\.?)?",
        re.I,
    ),
)


def _parse_hour_window(text: str) -> list[int] | None:
    """Parse a half-open [start, end) hour window from natural-language text."""
    for pattern in _HOUR_WINDOW_RES:
        m = pattern.search(text)
        if not m:
            continue
        groups = m.groupdict()
        try:
            a = int(groups["a"])
            b = int(groups["b"])
        except (KeyError, TypeError, ValueError):
            continue
        ap_a = (groups.get("ap_a") or groups.get("ap") or "").lower()
        ap_b = (groups.get("ap_b") or groups.get("ap") or "").lower()
        a24 = _to_24(a, ap_a)
        b24 = _to_24(b, ap_b)
        if a24 is None or b24 is None:
            continue
        # Half-open: [start, end). Clip to [0, 24].
        start, end = sorted((a24, b24))
        end = min(end, 24)
        if start >= end:
            continue
        hours = list(range(start, end))
        if hours and all(0 <= h <= 23 for h in hours):
            return hours
    return None


def _to_24(value: int, meridiem: str) -> int | None:
    if not meridiem:
        return value if 0 <= value <= 23 else None
    meridiem = meridiem.replace(".", "")
    if meridiem in ("am", "a"):
        return value % 12
    if meridiem in ("pm", "p"):
        v = value % 12
        return v + 12
    return None
