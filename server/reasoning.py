"""Reasoning steps run over fresh query results.

In the design the runner loops through an LLM service to run a report's reasoning
steps over freshly executed results and produce narrative. This POC keeps the
core path LLM-free with a deterministic engine behind a ReasoningEngine protocol,
mirroring how the compiler hides an LLM behind the Distiller protocol. An
LLM-backed engine can drop in behind the same interface without touching the
runner or the parity gate.

A reasoning step is {step_id, result_name, field, agg}. The engine computes the
aggregate over the fresh result and renders a short sentence. Because it runs on
whatever data the replay produced, the narrative changes with the report date.
"""

from __future__ import annotations

from typing import Protocol


class ReasoningEngine(Protocol):
    """Turn reasoning steps plus fresh results into narrative strings."""

    def run(self, steps: list[dict], results_by_name: dict[str, dict]) -> dict[str, str]:
        ...


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _round(value: float) -> float | int:
    rounded = round(float(value), 4)
    return int(rounded) if float(rounded).is_integer() else rounded


class HeuristicReasoningEngine:
    """Deterministic narrative generator, no LLM or network."""

    def run(
        self, steps: list[dict], results_by_name: dict[str, dict]
    ) -> dict[str, str]:
        narratives: dict[str, str] = {}
        for step in steps:
            narratives[step["step_id"]] = self._one(step, results_by_name)
        return narratives

    def _one(self, step: dict, results_by_name: dict[str, dict]) -> str:
        result = results_by_name.get(step["result_name"])
        if not result or not result["rows"]:
            return "No data available for this period."
        field = step["field"]
        agg = step.get("agg", "max")
        rows = result["rows"]
        numeric = [(row[field], row) for row in rows if _is_number(row.get(field))]
        if not numeric:
            return f"No numeric values found for {field}."
        label_col = next(
            (c for c in result["columns"] if not _is_number(rows[0].get(c))), None
        )
        n = len(rows)

        if agg in ("max", "min"):
            picker = max if agg == "max" else min
            value, row = picker(numeric, key=lambda pair: pair[0])
            superlative = "highest" if agg == "max" else "lowest"
            label = f" at {row[label_col]}" if label_col else ""
            return (
                f"Across {n} rows, the {superlative} {field} is "
                f"{_round(value)}{label}."
            )
        values = [value for value, _row in numeric]
        if agg == "total":
            return f"The total {field} across {n} rows is {_round(sum(values))}."
        # Default to average.
        return (
            f"The average {field} across {n} rows is "
            f"{_round(sum(values) / len(values))}."
        )


def get_engine() -> ReasoningEngine:
    """Return the reasoning engine for the core path (deterministic by default)."""
    return HeuristicReasoningEngine()
