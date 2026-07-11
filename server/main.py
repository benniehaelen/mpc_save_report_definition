"""FastMCP server entry point for the hin-poc server.

Starts a stdio MCP server exposing the four tools. Run directly:

    python server/main.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

# Allow "python server/main.py" by putting the project root on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb  # noqa: E402
from fastmcp import Context, FastMCP  # noqa: E402

from server import call_log, correlation, observability, tools  # noqa: E402
from server.db import DB_PATH, PROJECT_ROOT, get_connection, get_meta_connection  # noqa: E402

mcp = FastMCP("hin-poc")

_META_PROBE_PATH = PROJECT_ROOT / "logs" / "meta_probe.jsonl"

_GENERATED_WARNING = (
    "correlation is not established (no conversation_id and no recognized _meta "
    "trace id); pass conversation_id explicitly or use a client that sends _meta"
)


def _meta_dict(ctx: Context | None) -> dict:
    """The client's request `_meta` as a plain dict, or {} when absent.

    Guards both levels: `request_context` is None before a session is established,
    and `.meta` is None when the client sends no `_meta`. Reads arbitrary client
    keys (they survive as pydantic extras), including dotted ones like
    `vscode.conversationId`. Never raises -- diagnostics must not break a call.
    """
    try:
        request_context = getattr(ctx, "request_context", None)
        meta = getattr(request_context, "meta", None) if request_context else None
        return meta.model_dump(exclude_none=True) if meta is not None else {}
    except Exception:  # noqa: BLE001 - a probe read must never fail a tool call
        return {}


def _probe(tool: str, explicit: str | None, meta: dict) -> None:
    """When POC_LOG_META=1, record the raw _meta so its scope can be analyzed."""
    if os.environ.get("POC_LOG_META") != "1":
        return
    try:
        _META_PROBE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _META_PROBE_PATH.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "ts": dt.datetime.now().isoformat(timespec="seconds"),
                        "tool": tool,
                        "explicit_conversation_id": explicit,
                        "meta": meta or None,
                    }
                )
                + "\n"
            )
    except OSError:
        pass


def _resolve(tool: str, conversation_id: str | None, ctx: Context | None) -> tuple[str, str]:
    """Resolve the correlation key at the boundary and record diagnostics."""
    meta = _meta_dict(ctx)
    _probe(tool, conversation_id, meta)
    key, source = correlation.resolve(conversation_id, meta)
    observability.log_span(
        f"tool.{tool}", correlation_source=source, correlation_key=key[:12]
    )
    return key, source


def _with_generated_warning(result: dict, source: str) -> dict:
    """Append a warning to any tool response when correlation was not established."""
    if source == "generated" and isinstance(result, dict):
        result.setdefault("warnings", []).append(_GENERATED_WARNING)
    return result


def _open_databases_or_exit() -> None:
    """Open both databases at startup, or exit with a clear message.

    The warehouse is opened read-only, which takes a *shared* lock: several
    servers, the replay runner, pytest and harlequin may all hold it at once.
    Writes go to the SQLite metadata store, which allows one writer alongside
    concurrent readers. So there is no exclusive lock to contend for, and no
    reason to refuse a second server.

    This runs at startup purely to fail loudly on a missing or unreadable
    database -- otherwise the first tool call would throw and the client would
    see it as the tools hanging -- and to warm both connections.
    """
    try:
        get_connection(read_only=True)
        get_meta_connection()
    except duckdb.IOException as exc:
        sys.exit(
            f"hin-poc: cannot open {DB_PATH}: {exc}\n"
            "The warehouse is opened read-only, so this usually means another "
            "process holds it read-write -- most likely 'python data/seed.py' "
            "mid-run. Wait for it to finish, then start the server again."
        )
    except FileNotFoundError as exc:
        sys.exit(f"hin-poc: {exc}")


@mcp.tool
def nl_query(question: str, conversation_id: str | None = None, ctx: Context = None) -> dict:
    """Return query context (intent, relevant tables, suggested SQL) for a
    natural language question. Does not execute anything.

    conversation_id is optional; when omitted, the session is identified from the
    client's _meta trace id, so all calls from one chat correlate automatically."""
    key, source = _resolve("nl_query", conversation_id, ctx)
    return _with_generated_warning(tools.nl_query(key, question), source)


@mcp.tool
def dry_run_sql(sql: str, conversation_id: str | None = None, ctx: Context = None) -> dict:
    """Validate a single SELECT and return its result schema without executing
    side effects. Not recorded as lineage.

    conversation_id is optional; when omitted, the session is identified from the
    client's _meta trace id, so all calls from one chat correlate automatically."""
    key, source = _resolve("dry_run_sql", conversation_id, ctx)
    return _with_generated_warning(tools.dry_run_sql(key, sql), source)


@mcp.tool
def execute_sql(
    sql: str, result_name: str, conversation_id: str | None = None, ctx: Context = None
) -> dict:
    """Execute a validated SELECT (capped at 500 rows), assign it result_name,
    log the call, and return the rows.

    conversation_id is optional; when omitted, the session is identified from the
    client's _meta trace id, so all calls from one chat correlate automatically."""
    key, source = _resolve("execute_sql", conversation_id, ctx)
    return _with_generated_warning(tools.execute_sql(key, sql, result_name), source)


@mcp.tool
def save_report_definition(
    report_name: str,
    transcript: list[dict],
    final_artifact: dict,
    temporal_confirmations: list[dict] | None = None,
    structure_confirmations: list[dict] | None = None,
    conversation_id: str | None = None,
    ctx: Context = None,
) -> dict:
    """Distill a named query set from the session, run the parity gate against
    the final artifact, and register the definition on pass.

    A free-form artifact (no contract markup) is normalized first by save-time
    structure extraction. If that rests on any inference the call returns
    `needs_structure_confirmation` with a `confirmation_token`; pass it back in
    `structure_confirmations`, optionally with per-item overrides, e.g.
    `[{"token": "...", "accept_all": true}]`.

    conversation_id is optional; when omitted, the session is identified from the
    client's _meta trace id. The response's `session` block reports which key was
    used, its source, and how many logged queries were found under it."""
    key, source = _resolve("save_report_definition", conversation_id, ctx)
    logged_calls = len(call_log.fetch(get_meta_connection(), key))
    session_block = {
        "correlation_key": key[:12] + "...",
        "source": source,
        "logged_calls": logged_calls,
    }

    if logged_calls == 0:
        # A report always references at least one result, so zero logged calls
        # under this key is a guaranteed failure. Say why, and how to fix it,
        # instead of letting it surface as an opaque parity_failed.
        return _with_generated_warning(
            {
                "status": "no_logged_calls",
                "report_id": None,
                "definition_version": None,
                "error": (
                    f"0 executed queries found for session {key[:12]}... "
                    f"(source: {source}). If this chat was reloaded or the queries "
                    "ran in a different conversation, re-run the execute_sql calls "
                    "in this chat and save again."
                ),
                "session": session_block,
            },
            source,
        )

    result = tools.save_report_definition(
        key,
        report_name,
        transcript,
        final_artifact,
        temporal_confirmations,
        structure_confirmations,
    )
    if isinstance(result, dict):
        result["session"] = session_block
    return _with_generated_warning(result, source)


if __name__ == "__main__":
    _open_databases_or_exit()
    mcp.run()
