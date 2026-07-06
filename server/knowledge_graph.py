"""The local knowledge graph: governed metrics and their ValueSets.

This is the cloud-free stand-in for the Knowledge graph in the design. It holds
the metric catalog and the ValueSet memberships that metric and dimension
bindings are validated against, both when a definition is saved and again when
the runner replays it ("fetch definition, validate bindings").
"""

from __future__ import annotations

import duckdb


def load_catalog(con: duckdb.DuckDBPyConnection) -> dict:
    """Load the metric ids, ValueSet memberships, and dimension mappings."""
    metrics = {
        row[0] for row in con.execute("SELECT metric_id FROM metrics").fetchall()
    }
    value_sets: dict[str, set[str]] = {}
    for name, code in con.execute("SELECT value_set, code FROM value_sets").fetchall():
        value_sets.setdefault(name, set()).add(code)
    dimensions = {
        dim: vs
        for dim, vs in con.execute(
            "SELECT dimension, value_set FROM dimension_value_sets"
        ).fetchall()
    }
    return {"metrics": metrics, "value_sets": value_sets, "dimensions": dimensions}


def validate_bindings(catalog: dict, bindings: list[dict]) -> list[str]:
    """Return a list of validation errors for the given metric bindings.

    A binding names either a governed metric or a ValueSet-governed dimension.
    Anything referencing an unknown metric or ValueSet is an error.
    """
    errors: list[str] = []
    for binding in bindings:
        metric_id = binding.get("metric_id")
        value_set = binding.get("value_set")
        field = binding.get("field", "?")
        if metric_id is not None and metric_id not in catalog["metrics"]:
            errors.append(f"field '{field}' binds unknown metric '{metric_id}'")
        if value_set is not None and value_set not in catalog["value_sets"]:
            errors.append(f"field '{field}' binds unknown ValueSet '{value_set}'")
    return errors
