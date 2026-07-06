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

import os
from typing import Protocol

# Model used by the optional LLM engine. Overridable, defaults to Opus 4.8.
_LLM_MODEL = os.environ.get("POC_REASONING_MODEL", "claude-opus-4-8")
_MAX_TABLE_ROWS = 50


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


class AnthropicReasoningEngine:
    """Optional LLM-backed engine behind the same protocol.

    This is the design's LLM service, wired in without touching the runner or the
    parity gate. It is opt-in (see get_engine) and never used by the tests or the
    default core path. Any failure (no SDK, no credentials, network, empty
    response) falls back to the deterministic engine, so a replay never breaks.
    """

    def __init__(self, model: str = _LLM_MODEL) -> None:
        self._model = model
        self._fallback = HeuristicReasoningEngine()

    _SYSTEM = (
        "You write a single concise sentence of insight about a SQL query "
        "result. No preamble, exactly one sentence, grounded in the numbers."
    )

    def run(
        self, steps: list[dict], results_by_name: dict[str, dict]
    ) -> dict[str, str]:
        if not steps:
            return {}
        try:
            import anthropic

            client = anthropic.Anthropic()
        except Exception:  # noqa: BLE001 - no SDK or no credentials
            return self._fallback.run(steps, results_by_name)

        narratives: dict[str, str] = {}
        for step in steps:
            try:
                narratives[step["step_id"]] = self._one(client, step, results_by_name)
            except Exception:  # noqa: BLE001 - fall back per step on any error
                narratives[step["step_id"]] = self._fallback._one(
                    step, results_by_name
                )
        return narratives

    def _one(self, client, step: dict, results_by_name: dict[str, dict]) -> str:
        result = results_by_name.get(step["result_name"])
        if not result or not result["rows"]:
            return self._fallback._one(step, results_by_name)
        columns = result["columns"]
        rows = result["rows"][:_MAX_TABLE_ROWS]
        table = "\n".join(
            [" | ".join(columns)]
            + [" | ".join(str(row[c]) for c in columns) for row in rows]
        )
        user = (
            f"Result '{step['result_name']}' "
            f"(aggregate of interest: {step['agg']} of {step['field']}):\n"
            f"{table}\n\nWrite one concise sentence of insight."
        )
        response = client.messages.create(
            model=self._model,
            max_tokens=300,
            thinking={"type": "adaptive"},
            output_config={"effort": "low"},
            system=self._SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        text = next((b.text for b in response.content if b.type == "text"), "").strip()
        return text or self._fallback._one(step, results_by_name)


def get_engine() -> ReasoningEngine:
    """Return the reasoning engine.

    Deterministic by default so the core path stays LLM-free and offline. Set
    POC_REASONING=anthropic (and install the 'llm' extra plus configure Anthropic
    credentials) to opt into the LLM-backed engine, which falls back to the
    heuristic on any error.
    """
    mode = os.environ.get("POC_REASONING", "heuristic").lower()
    if mode in ("anthropic", "llm", "claude"):
        return AnthropicReasoningEngine()
    return HeuristicReasoningEngine()
