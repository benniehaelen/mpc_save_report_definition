"""Reasoning steps run over fresh query results.

In the design the runner loops through an LLM service to run a report's reasoning
steps over freshly executed results and produce narrative. This POC keeps the
core path LLM-free with a deterministic engine behind a ReasoningEngine protocol,
mirroring how the compiler hides an LLM behind the Distiller protocol. An
LLM-backed engine can drop in behind the same interface without touching the
runner or the parity gate.

There are two step schemas, and an engine tells them apart by the presence of a
``goal`` key:

* v1 -- ``{step_id, result_name, field, agg}``. The engine computes one aggregate
  over one result and renders a sentence about it.
* v2 -- ``{step_id, goal, inputs, max_sentences, style}``, where each input is
  ``{result_name, filter}``. The step states what it wants explained rather than
  which aggregate to take, so a step can span several results.

Because either kind runs on whatever data the replay produced, the narrative
changes with the report date. Reasoning prose is never parity-checked; only the
numbers it is derived from are.
"""

from __future__ import annotations

import os
from typing import Protocol

from server.env import load_dotenv

# Load a local .env (if present) so ANTHROPIC_API_KEY / POC_REASONING can be set
# there. A no-op when no .env exists, so the default offline path is unchanged.
load_dotenv()

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


def _is_v2(step: dict) -> bool:
    """v2 steps state a goal; v1 steps name a result_name/field/agg."""
    return "goal" in step


def _filtered_rows(result: dict, filter_spec: dict | None) -> list[dict]:
    rows = result["rows"]
    if not filter_spec:
        return rows
    column, value = filter_spec["col"], filter_spec["val"]
    return [row for row in rows if str(row.get(column)) == value]


def _describe_input(inp: dict, results_by_name: dict[str, dict]) -> str:
    """One grounded sentence about one input, or a clean 'no data' sentence."""
    name = inp["result_name"]
    result = results_by_name.get(name)
    if not result or not result["rows"]:
        return f"{name}: no data available for this period."
    rows = _filtered_rows(result, inp.get("filter"))
    if not rows:
        return f"{name}: no rows match the requested filter."
    field = next(
        (c for c in result["columns"] if _is_number(rows[0].get(c))), None
    )
    if field is None:
        return f"{name}: {len(rows)} rows; no numeric field to summarize."
    values = [row[field] for row in rows if _is_number(row.get(field))]
    average = _round(sum(values) / len(values))
    return (
        f"{name}: {len(rows)} rows; {field} ranges "
        f"{_round(min(values))}–{_round(max(values))} (avg {average})."
    )


class HeuristicReasoningEngine:
    """Deterministic narrative generator, no LLM or network."""

    def run(
        self, steps: list[dict], results_by_name: dict[str, dict]
    ) -> dict[str, str]:
        narratives: dict[str, str] = {}
        for step in steps:
            narratives[step["step_id"]] = (
                self._one_v2(step, results_by_name)
                if _is_v2(step)
                else self._one(step, results_by_name)
            )
        return narratives

    def _one_v2(self, step: dict, results_by_name: dict[str, dict]) -> str:
        """One sentence per input, capped at max_sentences.

        This engine exists so the default path and the test suite never reach for
        a network. It describes the inputs rather than interpreting them; the
        LLM engine is what turns the same inputs into an argument.
        """
        limit = int(step.get("max_sentences", 3))
        sentences = [
            _describe_input(inp, results_by_name) for inp in step["inputs"][:limit]
        ]
        return " ".join(sentences)

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
            v2 = _is_v2(step)
            try:
                narratives[step["step_id"]] = (
                    self._one_v2(client, step, results_by_name)
                    if v2
                    else self._one(client, step, results_by_name)
                )
            except Exception:  # noqa: BLE001 - fall back per step on any error
                fallback = self._fallback._one_v2 if v2 else self._fallback._one
                narratives[step["step_id"]] = fallback(
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

    _SYSTEM_V2 = (
        "You write a short, decision-oriented narrative about SQL query results. "
        "No preamble. Ground every claim strictly in the numbers you are given, "
        "and never infer causality."
    )

    def _one_v2(self, client, step: dict, results_by_name: dict[str, dict]) -> str:
        blocks: list[str] = []
        for inp in step["inputs"]:
            name = inp["result_name"]
            result = results_by_name.get(name)
            if not result or not result["rows"]:
                continue
            rows = _filtered_rows(result, inp.get("filter"))[:_MAX_TABLE_ROWS]
            if not rows:
                continue
            columns = result["columns"]
            table = "\n".join(
                [" | ".join(columns)]
                + [" | ".join(str(row[c]) for c in columns) for row in rows]
            )
            suffix = (
                f" (filtered to {inp['filter']['col']}={inp['filter']['val']})"
                if inp.get("filter")
                else ""
            )
            blocks.append(f"## {name}{suffix}\n{table}")
        if not blocks:
            return self._fallback._one_v2(step, results_by_name)

        max_sentences = int(step.get("max_sentences", 3))
        user = (
            f"{step['goal']}\n\n"
            + "\n\n".join(blocks)
            + f"\n\nWrite at most {max_sentences} sentences, grounded strictly in "
            "these numbers."
        )
        response = client.messages.create(
            model=self._model,
            max_tokens=600,
            thinking={"type": "adaptive"},
            output_config={"effort": "low"},
            system=self._SYSTEM_V2,
            messages=[{"role": "user", "content": user}],
        )
        text = next((b.text for b in response.content if b.type == "text"), "").strip()
        return text or self._fallback._one_v2(step, results_by_name)


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
