"""The four MCP tools, implemented as plain functions.

main.py wires these into FastMCP. Every tool takes conversation_id as its first
parameter; the POC treats it as an opaque session key. The functions get their
DuckDB connection from server.db so tests can call them in-process.
"""

from __future__ import annotations

import json
import re

from server import (
    artifact,
    call_log,
    compiler,
    extraction_cache,
    extractor,
    fingerprint,
    intent_catalog,
    knowledge_graph,
    normalizer,
    parity,
    registry,
)
from server.db import (
    ANCHOR_DATE,
    execute_params,
    get_connection,
    get_meta_connection,
)

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
        cols = execute_params(
            con,
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


def execute_logged(
    conversation_id: str, sql: str, result_name: str, tool_name: str = "execute_sql"
) -> dict:
    """Execute a validated SELECT (row-capped), fingerprint it, and log the call.

    ``tool_name`` distinguishes lineage sources: the ``execute_sql`` tool, versus
    ``save_derive`` for a query the structure extractor proposed and the server
    verified at save time. Both are real lineage -- the compiler matches result
    names against this log regardless of which tool produced them.
    """
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
    fingerprint, canonical = call_log.fingerprint_result(columns, rows)
    call_log.log_call(
        get_meta_connection(),
        conversation_id,
        tool_name,
        sql,
        result_name,
        len(rows),
        result_fingerprint=fingerprint,
        result_rows=canonical,
    )
    return {
        "result_name": result_name,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
    }


def execute_sql(conversation_id: str, sql: str, result_name: str) -> dict:
    """Execute a validated SELECT (capped at 500 rows) and log the call."""
    return execute_logged(conversation_id, sql, result_name, "execute_sql")


def _extraction_summary(plan: dict, report) -> dict:
    """What the client is being asked to confirm, in human-readable terms."""
    blob_names = {b.blob_id: b.describe() for b in report.blobs}
    return {
        "matched_islands": [
            {"result_name": i["result_name"], "source": blob_names.get(i["blob_id"], i["blob_id"])}
            for i in plan.get("islands", [])
            if i.get("origin") == "fingerprint"
        ],
        "proposed_islands": [
            {"result_name": i["result_name"], "source": blob_names.get(i["blob_id"], i["blob_id"])}
            for i in plan.get("islands", [])
            if i.get("origin") != "fingerprint"
        ],
        "derived_queries": [
            {"result_name": d["result_name"], "sql": d["sql"], "covers": d.get("covers", [])}
            for d in plan.get("derived_queries", [])
        ],
        "charts": plan.get("charts", []),
        "narrative": [
            {
                "block_id": n["block_id"],
                "tier": n.get("tier", "editorial"),
                "excerpt": n.get("excerpt", ""),
                **({"goal": n["goal"]} if n.get("goal") else {}),
            }
            for n in plan.get("narrative", [])
        ],
        "unmatched": [b.describe() for b in report.unmatched]
        + [f"const {n} is computed in JavaScript" for n in report.unparseable]
        + [f"unresolved number {s.raw_text!r}" for s in report.unresolved_values],
    }


def _apply_overrides(plan: dict, confirmations: list[dict]) -> dict:
    """Fold the client's per-item decisions into the cached plan."""
    plan = json.loads(json.dumps(plan))  # deep copy; the cached plan stays pristine
    accept_all = any(c.get("accept_all") for c in confirmations)

    accepted: set[str] = set()
    rejected: set[str] = set()
    for entry in confirmations:
        if "derived" in entry:
            (accepted if entry.get("accept", True) else rejected).add(entry["derived"])
        if "block_id" in entry and entry.get("tier"):
            for block in plan.get("narrative", []):
                if block["block_id"] == entry["block_id"]:
                    block["tier"] = entry["tier"]
                    if entry.get("goal"):
                        block["goal"] = entry["goal"]

    proposed = plan.get("derived_queries", [])
    if accept_all:
        kept = [d for d in proposed if d["result_name"] not in rejected]
    else:
        kept = [d for d in proposed if d["result_name"] in accepted]
    plan["derived_queries"] = kept

    # A rejected derived query takes its dependents with it. Its rows exist in the
    # log (validation executed it to prove coverage), so an island bound to it
    # would otherwise still be built from data the client just declined.
    dropped = {d["result_name"] for d in proposed} - {d["result_name"] for d in kept}
    if dropped:
        plan["islands"] = [i for i in plan.get("islands", []) if i["result_name"] not in dropped]
        plan["values"] = [v for v in plan.get("values", []) if v.get("result") not in dropped]
        plan["charts"] = [c for c in plan.get("charts", []) if c.get("result") not in dropped]
    return plan


def _normalize_free_form(
    conversation_id: str,
    final_artifact: dict,
    structure_confirmations: list[dict] | None,
) -> tuple[dict | None, dict, list[str], str | None]:
    """Fingerprint, propose, confirm, and rewrite a free-form artifact.

    Returns ``(early_response, normalized_artifact, warnings, token)``. When
    ``early_response`` is not None the caller must return it unchanged: the plan
    contains inferences the client has not yet confirmed, and nothing may be
    registered on inference alone. ``token`` names the confirmed plan, so the
    caller can mark it consumed once a definition is registered.
    """
    meta = get_meta_connection()
    html = final_artifact.get("content", "")
    calls = call_log.fetch(meta, conversation_id)
    report = fingerprint.match(html, calls)
    session = extractor.SessionContext(
        conversation_id=conversation_id,
        calls=calls,
        anchor_date=ANCHOR_DATE,
        con=get_connection(),
        meta=meta,
    )
    token: str | None = None

    if structure_confirmations:
        token = next((c.get("token") for c in structure_confirmations if c.get("token")), None)
        if not token:
            return (
                {
                    "status": "invalid_structure_confirmation",
                    "error": "structure_confirmations must carry the confirmation_token "
                    "returned by the first save call",
                },
                final_artifact,
                [],
                None,
            )
        cached = extraction_cache.load(meta, token)
        if cached is None or cached["conversation_id"] != conversation_id:
            return (
                {
                    "status": "structure_confirmation_expired",
                    "error": f"no cached extraction plan for token {token[:12]}... in this "
                    "conversation; call save_report_definition again to get a fresh proposal",
                },
                final_artifact,
                [],
                None,
            )
        if cached["consumed_at"]:
            # A token is single-use. Re-presenting it would quietly register a
            # second version of a report the client already has.
            return (
                {
                    "status": "structure_confirmation_used",
                    "error": f"this confirmation token was already used on "
                    f"{cached['consumed_at']} to register "
                    f"{cached['report_id']!r}; call save_report_definition without "
                    "structure_confirmations to get a fresh proposal",
                    "report_id": cached["report_id"],
                },
                final_artifact,
                [],
                None,
            )
        if cached["html_sha256"] != extraction_cache.html_token(html):
            # The plan is a set of offsets into the HTML it was proposed against.
            # Splicing it into an edited page would cut at the wrong bytes.
            return (
                {
                    "status": "structure_confirmation_stale",
                    "error": "the artifact changed since this plan was proposed; "
                    "call save_report_definition without structure_confirmations "
                    "to get a fresh proposal for the new page",
                },
                final_artifact,
                [],
                None,
            )
        plan = cached["plan"]
        warnings = list(plan.get("_warnings", []))
        plan = _apply_overrides(plan, structure_confirmations)
    else:
        proposed = extractor.get_extractor().propose(html, report, session)
        plan, warnings = extractor.validate_plan(proposed, report, session)
        if extractor.has_inferences(plan):
            plan["_warnings"] = warnings
            token, created_at = extraction_cache.new_token(plan, conversation_id)
            extraction_cache.save(
                meta,
                token,
                conversation_id,
                plan,
                extraction_cache.html_token(html),
                created_at,
            )
            return (
                {
                    "status": "needs_structure_confirmation",
                    "report_id": None,
                    "definition_version": None,
                    "extraction": _extraction_summary(plan, report),
                    "confirmation_token": token,
                    "warnings": warnings,
                },
                final_artifact,
                warnings,
                token,
            )

    # Derived queries were executed (and logged as save_derive) during validation,
    # so refetch to pick up their rows before building islands from them.
    calls = call_log.fetch(meta, conversation_id)
    logged_rows = _rows_by_result(calls)
    normalized_html, summary = normalizer.normalize(
        html, plan, logged_rows, report, save_date=ANCHOR_DATE
    )
    normalized = {**final_artifact, "content": normalized_html}
    return (
        None,
        normalized,
        warnings + summary.warnings + [f"unextracted: {item}" for item in summary.unmatched],
        token,
    )


def _rows_by_result(calls: list[dict]) -> dict[str, list[dict]]:
    """Latest logged rows per result name, as row dicts."""
    rows: dict[str, list[dict]] = {}
    for call in calls:
        payload = call.get("result_rows")
        name = call.get("result_name")
        if not name or not payload:
            continue
        columns = payload["columns"]
        rows[name] = [dict(zip(columns, row)) for row in payload["rows"]]
    return rows


def save_report_definition(
    conversation_id: str,
    report_name: str,
    transcript: list[dict],
    final_artifact: dict,
    temporal_confirmations: list[dict] | None = None,
    structure_confirmations: list[dict] | None = None,
) -> dict:
    """Distill a definition, run the parity gate, and register on pass.

    A v2-contract or legacy artifact goes straight to the compiler, exactly as
    before. A **free-form** artifact is first normalized into the v2 contract by
    save-time structure extraction; if that normalization rests on any inference,
    the call returns `needs_structure_confirmation` and registers nothing.
    """
    con = get_connection()
    meta = get_meta_connection()
    catalog = knowledge_graph.load_catalog(con)

    extraction_warnings: list[str] = []
    confirmed_token: str | None = None
    if artifact.detect_mode(final_artifact.get("content", "")) == "free_form":
        early, final_artifact, extraction_warnings, confirmed_token = _normalize_free_form(
            conversation_id, final_artifact, structure_confirmations
        )
        if early is not None:
            return early

    log_rows = call_log.fetch(meta, conversation_id)
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
    definition["warnings"] = extraction_warnings + definition["warnings"]

    parity_block = {
        "passed": parity_result["passed"],
        "attempts": attempts,
        "diff_summary": parity_result["diff_summary"],
    }
    if not parity_result["passed"]:
        # The token stays unspent: the plan was fine, the artifact was not, and the
        # client should be able to retry the same confirmed plan.
        return {
            "status": "parity_failed",
            "report_id": None,
            "definition_version": None,
            "parity": parity_block,
            "warnings": definition.get("warnings", []),
            "unreplayable_sections": definition.get("unreplayable_sections", []),
        }

    report_id, version = registry.register(meta, report_name, definition, attempts)
    if confirmed_token:
        extraction_cache.mark_consumed(meta, confirmed_token, report_id)
    return {
        "status": "registered",
        "report_id": report_id,
        "definition_version": version,
        "parity": parity_block,
        "warnings": definition.get("warnings", []),
        "unreplayable_sections": definition.get("unreplayable_sections", []),
    }
