"""The four MCP tools, implemented as plain functions.

main.py wires these into FastMCP. Every tool takes conversation_id as its first
parameter; the POC treats it as an opaque session key. The functions get their
DuckDB connection from server.db so tests can call them in-process.
"""

from __future__ import annotations

import re

from server import (
    call_log,
    compiler,
    intent_catalog,
    knowledge_graph,
    parity,
    registry,
)
from server.db import ANCHOR_DATE, get_connection

_ROW_CAP = 500
_FORBIDDEN_START = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|ATTACH|COPY|PRAGMA|CALL|"
    r"TRUNCATE|REPLACE|GRANT|REVOKE|EXPORT|INSTALL|LOAD|SET)\b",
    re.IGNORECASE,
)


def _validate_select(sql: str) -> tuple[bool, str | None]:
    """Accept only a single SELECT (or WITH) statement, no side effects."""
    stripped = sql.strip()
    if stripped.endswith(";"):
        stripped = stripped[:-1].strip()
    if ";" in stripped:
        return False, "Only a single statement is allowed (no semicolons)."
    if not stripped:
        return False, "Empty statement."
    if not re.match(r"^\s*(SELECT|WITH)\b", stripped, re.IGNORECASE):
        return False, "Only SELECT statements are allowed."
    if _FORBIDDEN_START.match(stripped):
        return False, "DDL and DML statements are not allowed."
    return True, None


def _schema_catalog(con) -> dict[str, list[str]]:
    tables = con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' ORDER BY table_name"
    ).fetchall()
    catalog: dict[str, list[str]] = {}
    for (table_name,) in tables:
        cols = con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ? ORDER BY ordinal_position",
            [table_name],
        ).fetchall()
        catalog[table_name] = [c[0] for c in cols]
    return catalog


def nl_query(conversation_id: str, question: str) -> dict:
    """Stand-in for the resolver pipeline: keyword intent to suggested SQL."""
    con = get_connection()
    catalog = _schema_catalog(con)
    intent = intent_catalog.match(question)
    if intent is None:
        return {
            "matched_intent": None,
            "relevant_tables": catalog,
            "suggested_sql": None,
            "note": "No intent matched; returning the full schema catalog.",
        }
    relevant = {t: catalog.get(t, []) for t in intent["tables"]}
    return {
        "matched_intent": intent["name"],
        "relevant_tables": relevant,
        "suggested_sql": intent["sql"],
    }


def dry_run_sql(conversation_id: str, sql: str) -> dict:
    """Validate without side effects; return the result schema. Not logged."""
    ok, error = _validate_select(sql)
    if not ok:
        return {"valid": False, "result_columns": [], "error": error}
    con = get_connection()
    try:
        con.execute(f"EXPLAIN {sql}")
        con.execute(f"SELECT * FROM ({sql}) AS _q LIMIT 0")
    except Exception as exc:  # noqa: BLE001 - surface DuckDB errors to the caller
        return {"valid": False, "result_columns": [], "error": str(exc)}
    columns = [{"name": d[0], "type": str(d[1])} for d in con.description]
    return {"valid": True, "result_columns": columns, "error": None}


def execute_sql(conversation_id: str, sql: str, result_name: str) -> dict:
    """Execute a validated SELECT (capped at 500 rows) and log the call."""
    ok, error = _validate_select(sql)
    if not ok:
        return {"error": error, "result_name": result_name}
    con = get_connection()
    try:
        con.execute(f"SELECT * FROM ({sql}) AS _q LIMIT {_ROW_CAP + 1}")
        fetched = con.fetchall()
        columns = [d[0] for d in con.description]
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "result_name": result_name}

    truncated = len(fetched) > _ROW_CAP
    fetched = fetched[:_ROW_CAP]
    rows = [dict(zip(columns, row)) for row in fetched]
    call_log.log_call(
        con, conversation_id, "execute_sql", sql, result_name, len(rows)
    )
    return {
        "result_name": result_name,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
    }


def save_report_definition(
    conversation_id: str,
    report_name: str,
    transcript: list[dict],
    final_artifact: dict,
    temporal_confirmations: list[dict] | None = None,
) -> dict:
    """Distill a definition, run the parity gate, and register on pass."""
    con = get_connection()
    log_rows = call_log.fetch(con, conversation_id)
    catalog = knowledge_graph.load_catalog(con)

    definition: dict = {}
    parity_result = {"passed": False, "diff_summary": "no attempt made"}
    attempts = 0
    for attempt in range(1, 4):
        attempts = attempt
        definition = compiler.distill(
            report_name=report_name,
            transcript=transcript,
            final_artifact=final_artifact,
            log_rows=log_rows,
            anchor_date=ANCHOR_DATE,
            catalog=catalog,
            temporal_confirmations=temporal_confirmations,
            attempt=attempt,
        )
        parity_result = parity.check(con, definition, final_artifact, ANCHOR_DATE)
        if parity_result["passed"]:
            break

    # Validate the distilled metric bindings against the knowledge graph.
    binding_errors = knowledge_graph.validate_bindings(
        catalog, definition.get("metric_bindings", [])
    )
    definition["warnings"] = definition.get("warnings", []) + [
        f"binding validation: {err}" for err in binding_errors
    ]

    parity_block = {
        "passed": parity_result["passed"],
        "attempts": attempts,
        "diff_summary": parity_result["diff_summary"],
    }
    if not parity_result["passed"]:
        return {
            "status": "parity_failed",
            "report_id": None,
            "definition_version": None,
            "parity": parity_block,
            "warnings": definition.get("warnings", []),
            "unreplayable_sections": definition.get("unreplayable_sections", []),
        }

    report_id, version = registry.register(con, report_name, definition, attempts)
    return {
        "status": "registered",
        "report_id": report_id,
        "definition_version": version,
        "parity": parity_block,
        "warnings": definition.get("warnings", []),
        "unreplayable_sections": definition.get("unreplayable_sections", []),
    }
