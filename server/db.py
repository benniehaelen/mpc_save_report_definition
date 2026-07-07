"""Shared database path, connection helpers, and project-wide constants.

A single small module so every other module agrees on where the DuckDB file
lives and what the fixed anchor date is. Connections are cached per
(path, read_only) pair so the server reuses one read-write connection.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "poc.duckdb"
TEMPLATES_DIR = PROJECT_ROOT / "templates"

# Fixed anchor date the synthetic data ends on. Kept in sync with data/seed.py.
ANCHOR_DATE = "2025-06-30"

_CONNECTIONS: dict[tuple[str, bool], duckdb.DuckDBPyConnection] = {}


def get_connection(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Return a cached DuckDB connection.

    The server uses a read-write connection. DuckDB takes an exclusive
    OS-level lock in read-write mode, so a second process (such as the replay
    runner) cannot open the same file while the server is running -- not even
    read-only. Stop the server before running the runner.
    """
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"{DB_PATH} does not exist. Run 'python data/seed.py' first."
        )
    key = (str(DB_PATH), read_only)
    con = _CONNECTIONS.get(key)
    if con is None:
        con = duckdb.connect(str(DB_PATH), read_only=read_only)
        _CONNECTIONS[key] = con
    return con


def _sql_literal(value) -> str:
    """Render a Python value as a DuckDB SQL literal.

    Strings are single-quoted with embedded quotes doubled, which contains
    string-literal injection. Only the handful of types our internal queries
    bind (str, int, float, bool, date/datetime, None) are supported.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, dt.datetime):
        return "TIMESTAMP '" + value.isoformat(sep=" ") + "'"
    if isinstance(value, dt.date):
        return "DATE '" + value.isoformat() + "'"
    return "'" + str(value).replace("'", "''") + "'"


def execute_params(con, sql: str, params) -> "duckdb.DuckDBPyConnection":
    """Execute a query, inlining ``?`` placeholders as SQL literals.

    Works around a duckdb 1.5.4 deadlock: a ``?``-parameterized query
    (prepared statement) hangs indefinitely when executed inside the FastMCP
    server's tool-worker thread, while the identical query with literals runs
    fine. The bug does not reproduce in a plain interpreter, only under the MCP
    server's threaded execution, which is why every DB write/read on the server
    request path must avoid bound parameters. Each ``?`` (there are none inside
    string literals in our internal SQL) is replaced positionally.
    """
    parts = sql.split("?")
    if len(parts) - 1 != len(params):
        raise ValueError(
            f"expected {len(parts) - 1} params for query, got {len(params)}"
        )
    rendered = parts[0]
    for value, tail in zip(params, parts[1:]):
        rendered += _sql_literal(value) + tail
    return con.execute(rendered)
