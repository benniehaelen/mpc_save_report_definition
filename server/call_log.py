"""Tool-call log keyed by conversation_id.

Only execute_sql writes here: the log is the lineage record that lets the
compiler recover which named results a report was built from. Dry runs are not
lineage and never touch this table.

Lives in the SQLite metadata store (`server.db.META_PATH`), not the DuckDB
warehouse, so that writing lineage never takes an exclusive lock on the analytic
tables. See the module docstring in `server/db.py`.
"""

from __future__ import annotations

import datetime as dt
import sqlite3


def log_call(
    con: sqlite3.Connection,
    conversation_id: str,
    tool_name: str,
    sql_text: str,
    result_name: str,
    row_count: int,
) -> int:
    """Append a row to tool_call_log and return the assigned call_id."""
    cur = con.execute(
        "INSERT INTO tool_call_log "
        "(conversation_id, tool_name, sql_text, result_name, row_count, called_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            conversation_id,
            tool_name,
            sql_text,
            result_name,
            row_count,
            dt.datetime.now().isoformat(sep=" ", timespec="seconds"),
        ),
    )
    con.commit()
    return cur.lastrowid


def fetch(con: sqlite3.Connection, conversation_id: str) -> list[dict]:
    """Return this conversation's logged calls, oldest first."""
    cur = con.execute(
        """
        SELECT call_id, conversation_id, tool_name, sql_text, result_name,
               row_count, called_at
        FROM tool_call_log
        WHERE conversation_id = ?
        ORDER BY call_id
        """,
        (conversation_id,),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]
