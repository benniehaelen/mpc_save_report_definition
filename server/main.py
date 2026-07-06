"""FastMCP server entry point for the hin-poc server.

Starts a stdio MCP server exposing the four tools. Run directly:

    python server/main.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow "python server/main.py" by putting the project root on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastmcp import FastMCP  # noqa: E402

from server import tools  # noqa: E402

mcp = FastMCP("hin-poc")


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
    mcp.run()
