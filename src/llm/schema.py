"""JSON schema describing the LLM's structured output."""
from __future__ import annotations

import copy
from typing import Any


def _hours_array_schema() -> dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": "integer", "minimum": 0, "maximum": 23},
        "minItems": 1,
        "maxItems": 24,
    }


def _build_directive_entry_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["note_index", "applies", "directive_type", "explanation"],
        "properties": {
            "note_index": {"type": "integer", "minimum": 0},
            "applies": {"type": "boolean"},
            "directive_type": {
                "type": "string",
                "enum": [
                    "solar_reduction",
                    "minimum_battery_reserve",
                    "no_charge_window",
                    "no_discharge_window",
                    "max_grid_window",
                    "no_op",
                ],
            },
            "structured_adjustment": {
                "oneOf": [
                    {"type": "null"},
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "oneOf": [
                            {
                                "type": "object",
                                "required": ["hours", "factor"],
                                "properties": {
                                    "hours": _hours_array_schema(),
                                    "factor": {"type": "number", "minimum": 0, "maximum": 1},
                                },
                            },
                            {
                                "type": "object",
                                "required": ["hours", "minimum_energy_kwh"],
                                "properties": {
                                    "hours": _hours_array_schema(),
                                    "minimum_energy_kwh": {"type": "number", "minimum": 0},
                                },
                            },
                            {
                                "type": "object",
                                "required": ["hours"],
                                "properties": {"hours": _hours_array_schema()},
                            },
                            {
                                "type": "object",
                                "required": ["hours"],
                                "properties": {"hours": _hours_array_schema()},
                            },
                            {
                                "type": "object",
                                "required": ["hours", "max_grid_kwh"],
                                "properties": {
                                    "hours": _hours_array_schema(),
                                    "max_grid_kwh": {"type": "number", "minimum": 0},
                                },
                            },
                        ],
                    },
                ]
            },
            "explanation": {"type": "string"},
        },
    }


def _build_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["directives"],
        "properties": {
            "directives": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "items": _build_directive_entry_schema(),
            }
        },
    }


RESPONSE_SCHEMA: dict[str, Any] = _build_response_schema()


def build_response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "gridwise_directive_interpretation",
            "schema": copy.deepcopy(RESPONSE_SCHEMA),
            "strict": True,
        },
    }
