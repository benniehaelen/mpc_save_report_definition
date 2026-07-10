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
from server.db import DB_PATH, get_connection, get_meta_connection  # noqa: E402

mcp = FastMCP("hin-poc")


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
    _open_databases_or_exit()
    mcp.run()
