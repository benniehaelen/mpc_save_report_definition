"""Unit tests for the knowledge graph (metric bindings) and the reasoning engine."""

from __future__ import annotations

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
