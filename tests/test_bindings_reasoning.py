"""Unit tests for the knowledge graph (metric bindings) and the reasoning engine."""

from __future__ import annotations

import sys

from server import knowledge_graph, reasoning
from server.db import get_connection

_RESULTS = {
    "t": {
        "columns": ["label", "val"],
        "rows": [
            {"label": "A", "val": 3},
            {"label": "B", "val": 7},
            {"label": "C", "val": 5},
        ],
    }
}


def test_load_catalog_has_metrics_and_value_sets():
    catalog = knowledge_graph.load_catalog(get_connection())
    assert "occupancy_rate" in catalog["metrics"]
    assert catalog["value_sets"]["divisions"] == {"North", "Central", "South"}
    assert catalog["dimensions"]["division"] == "divisions"


def test_validate_bindings_accepts_known_and_flags_unknown():
    catalog = knowledge_graph.load_catalog(get_connection())
    good = [
        {"field": "occupancy_rate", "metric_id": "occupancy_rate", "value_set": None},
        {"field": "division", "metric_id": None, "value_set": "divisions"},
    ]
    assert knowledge_graph.validate_bindings(catalog, good) == []

    bad = [
        {"field": "mystery", "metric_id": "no_such_metric", "value_set": None},
        {"field": "division", "metric_id": None, "value_set": "no_such_set"},
    ]
    errors = knowledge_graph.validate_bindings(catalog, bad)
    assert len(errors) == 2
    assert any("no_such_metric" in e for e in errors)
    assert any("no_such_set" in e for e in errors)


def test_reasoning_aggregations():
    engine = reasoning.HeuristicReasoningEngine()
    steps = [
        {"step_id": "hi", "result_name": "t", "field": "val", "agg": "max"},
        {"step_id": "lo", "result_name": "t", "field": "val", "agg": "min"},
        {"step_id": "sum", "result_name": "t", "field": "val", "agg": "total"},
        {"step_id": "mean", "result_name": "t", "field": "val", "agg": "avg"},
    ]
    out = engine.run(steps, _RESULTS)
    assert out["hi"] == "Across 3 rows, the highest val is 7 at B."
    assert out["lo"] == "Across 3 rows, the lowest val is 3 at A."
    assert out["sum"] == "The total val across 3 rows is 15."
    assert out["mean"] == "The average val across 3 rows is 5."


def test_reasoning_is_deterministic():
    engine = reasoning.HeuristicReasoningEngine()
    steps = [{"step_id": "hi", "result_name": "t", "field": "val", "agg": "max"}]
    assert engine.run(steps, _RESULTS) == engine.run(steps, _RESULTS)


def test_reasoning_handles_empty_result():
    engine = reasoning.HeuristicReasoningEngine()
    steps = [{"step_id": "x", "result_name": "t", "field": "val", "agg": "max"}]
    empty = {"t": {"columns": ["label", "val"], "rows": []}}
    assert engine.run(steps, empty)["x"] == "No data available for this period."


def test_get_engine_defaults_to_heuristic(monkeypatch):
    monkeypatch.delenv("POC_REASONING", raising=False)
    assert isinstance(reasoning.get_engine(), reasoning.HeuristicReasoningEngine)


def test_get_engine_opts_into_llm(monkeypatch):
    monkeypatch.setenv("POC_REASONING", "anthropic")
    assert isinstance(reasoning.get_engine(), reasoning.AnthropicReasoningEngine)


def test_llm_engine_falls_back_without_sdk(monkeypatch):
    # Force `import anthropic` to fail so no network call is attempted; the LLM
    # engine must then produce the same output as the heuristic.
    monkeypatch.setitem(sys.modules, "anthropic", None)
    steps = [{"step_id": "hi", "result_name": "t", "field": "val", "agg": "max"}]
    llm = reasoning.AnthropicReasoningEngine()
    heuristic = reasoning.HeuristicReasoningEngine()
    assert llm.run(steps, _RESULTS) == heuristic.run(steps, _RESULTS)


# --- reasoning v2 --------------------------------------------------------
#
# A v2 step states a goal over named inputs instead of naming one aggregate.
# The heuristic engine describes those inputs deterministically so the default
# path never needs a network.

_V2_RESULTS = {
    "race": {
        "columns": ["qtr", "gap"],
        "rows": [
            {"qtr": "Q1'24", "gap": 1200},
            {"qtr": "Q2'24", "gap": 900},
            {"qtr": "Q3'24", "gap": 600},
        ],
    },
    "esl": {
        "columns": ["esl", "share_change_pp"],
        "rows": [
            {"esl": "ORTHOPEDICS", "share_change_pp": -2.7},
            {"esl": "GENERAL SURGERY", "share_change_pp": 2.3},
        ],
    },
}


def _v2_step(**overrides):
    step = {
        "step_id": "s",
        "goal": "Explain the competitive picture.",
        "inputs": [{"result_name": "race", "filter": None}],
        "max_sentences": 3,
        "style": None,
    }
    step.update(overrides)
    return step


def test_reasoning_v2_describes_each_input():
    engine = reasoning.HeuristicReasoningEngine()
    step = _v2_step(
        inputs=[
            {"result_name": "race", "filter": None},
            {"result_name": "esl", "filter": None},
        ]
    )
    out = engine.run([step], _V2_RESULTS)["s"]
    assert out == (
        "race: 3 rows; gap ranges 600–1200 (avg 900). "
        "esl: 2 rows; share_change_pp ranges -2.7–2.3 (avg -0.2)."
    )


def test_reasoning_v2_respects_filters():
    engine = reasoning.HeuristicReasoningEngine()
    step = _v2_step(inputs=[{"result_name": "race", "filter": {"col": "qtr", "val": "Q2'24"}}])
    out = engine.run([step], _V2_RESULTS)["s"]
    assert out == "race: 1 rows; gap ranges 900–900 (avg 900)."


def test_reasoning_v2_respects_max_sentences():
    engine = reasoning.HeuristicReasoningEngine()
    step = _v2_step(
        max_sentences=1,
        inputs=[
            {"result_name": "race", "filter": None},
            {"result_name": "esl", "filter": None},
        ],
    )
    out = engine.run([step], _V2_RESULTS)["s"]
    assert out.count(".") == 1
    assert "esl:" not in out


def test_reasoning_v2_is_deterministic():
    engine = reasoning.HeuristicReasoningEngine()
    step = _v2_step()
    assert engine.run([step], _V2_RESULTS) == engine.run([step], _V2_RESULTS)


def test_reasoning_v2_degrades_cleanly_on_missing_and_unmatched_data():
    engine = reasoning.HeuristicReasoningEngine()
    missing = _v2_step(step_id="a", inputs=[{"result_name": "nope", "filter": None}])
    unmatched = _v2_step(
        step_id="b",
        inputs=[{"result_name": "race", "filter": {"col": "qtr", "val": "Q9'99"}}],
    )
    out = engine.run([missing, unmatched], _V2_RESULTS)
    assert out["a"] == "nope: no data available for this period."
    assert out["b"] == "race: no rows match the requested filter."


def test_mixed_v1_and_v2_steps_run_together_and_v1_is_unchanged():
    engine = reasoning.HeuristicReasoningEngine()
    v1 = {"step_id": "old", "result_name": "t", "field": "val", "agg": "max"}
    v2 = _v2_step(step_id="new")
    out = engine.run([v1, v2], {**_RESULTS, **_V2_RESULTS})
    assert out["old"] == "Across 3 rows, the highest val is 7 at B."
    assert out["new"].startswith("race: 3 rows;")


def test_llm_engine_v2_falls_back_to_the_v2_heuristic_without_sdk(monkeypatch):
    """The per-step fallback must target _one_v2; a v2 step has no result_name."""
    monkeypatch.setitem(sys.modules, "anthropic", None)
    step = _v2_step()
    llm = reasoning.AnthropicReasoningEngine()
    heuristic = reasoning.HeuristicReasoningEngine()
    assert llm.run([step], _V2_RESULTS) == heuristic.run([step], _V2_RESULTS)
