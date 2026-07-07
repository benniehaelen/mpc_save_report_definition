"""FastMCP server entry point for the hin-poc server.

Starts a stdio MCP server exposing the four tools. Run directly:

    python server/main.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow "python server/main.py" by putting the project root on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb  # noqa: E402
from fastmcp import FastMCP  # noqa: E402

from server import tools  # noqa: E402
from server.db import DB_PATH, get_connection  # noqa: E402

mcp = FastMCP("hin-poc")


def _acquire_lock_or_exit() -> None:
    """Grab the read-write DB lock at startup, or exit with a clear message.

    DuckDB takes an exclusive OS-level lock in read-write mode, so only one
    hin-poc server can hold the file at a time. Without this check a second
    (usually orphaned) server would still start, then throw on every tool call
    because it can never open the database -- which looks to the client like the
    tools hanging. Failing loudly at startup instead means a duplicate server
    never sabotages a running session, and it warms the connection so the first
    tool call is fast.
    """
    try:
        get_connection(read_only=False)
    except duckdb.IOException:
        sys.exit(
            f"hin-poc: cannot open {DB_PATH}: it is locked by another process.\n"
            "Another hin-poc server (or the replay runner) already holds the "
            "database read-write. Stop that process first -- only one server can "
            "run at a time -- then start this one again."
        )
    except FileNotFoundError as exc:
        sys.exit(f"hin-poc: {exc}")


@mcp.tool
def nl_query(conversation_id: str, question: str) -> dict:
    """Return query context (intent, relevant tables, suggested SQL) for a
    natural language question. Does not execute anything."""
    return tools.nl_query(conversation_id, question)


@mcp.tool
def dry_run_sql(conversation_id: str, sql: str) -> dict:
    """Validate a single SELECT and return its result schema without executing
    side effects. Not recorded as lineage."""
    return tools.dry_run_sql(conversation_id, sql)


@mcp.tool
def execute_sql(conversation_id: str, sql: str, result_name: str) -> dict:
    """Execute a validated SELECT (capped at 500 rows), assign it result_name,
    log the call, and return the rows."""
    return tools.execute_sql(conversation_id, sql, result_name)


@mcp.tool
def save_report_definition(
    conversation_id: str,
    report_name: str,
    transcript: list[dict],
    final_artifact: dict,
    temporal_confirmations: list[dict] | None = None,
) -> dict:
    """Distill a named query set from the session, run the parity gate against
    the final artifact, and register the definition on pass."""
    return tools.save_report_definition(
        conversation_id,
        report_name,
        transcript,
        final_artifact,
        temporal_confirmations,
    )


if __name__ == "__main__":
    _acquire_lock_or_exit()
    mcp.run()
