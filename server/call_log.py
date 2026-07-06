"""Tool-call log keyed by conversation_id.

Only execute_sql writes here: the log is the lineage record that lets the
compiler recover which named results a report was built from. Dry runs are not
lineage and never touch this table.
"""

from __future__ import annotations

import datetime as dt

import duckdb


def log_call(
    con: duckdb.DuckDBPyConnection,
    conversation_id: str,
    tool_name: str,
    sql_text: str,
    result_name: str,
    row_count: int,
) -> int:
    """Append a row to tool_call_log and return the assigned call_id."""
    next_id = con.execute(
        "SELECT COALESCE(MAX(call_id), 0) + 1 FROM tool_call_log"
    ).fetchone()[0]
    con.execute(
        "INSERT INTO tool_call_log VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            next_id,
            conversation_id,
            tool_name,
            sql_text,
            result_name,
            row_count,
            dt.datetime.now(),
        ],
    )
    return next_id


def fetch(con: duckdb.DuckDBPyConnection, conversation_id: str) -> list[dict]:
    """Return this conversation's logged calls, oldest first."""
    rows = con.execute(
        """
        SELECT call_id, conversation_id, tool_name, sql_text, result_name,
               row_count, called_at
        FROM tool_call_log
        WHERE conversation_id = ?
        ORDER BY call_id
        """,
        [conversation_id],
    ).fetchall()
    cols = [d[0] for d in con.description]
    return [dict(zip(cols, row)) for row in rows]
