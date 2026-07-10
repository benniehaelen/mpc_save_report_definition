"""Shared database paths, connection helpers, and project-wide constants.

A single small module so every other module agrees on where the databases live
and what the fixed anchor date is.

Storage is split across two files, and the split is load-bearing:

* ``poc.duckdb`` holds the analytic warehouse and is **only ever opened
  read-only**. DuckDB grants a *shared* lock to read-only connections, so any
  number of processes -- the MCP server, the replay runner, pytest, harlequin --
  can attach at once. The moment one process opens it read-write it takes an
  *exclusive* lock and every other process is refused, read-only included.
* ``poc_meta.sqlite`` holds the two tables the server writes: the tool-call log
  and the definition registry. SQLite in WAL mode supports one writer alongside
  many concurrent readers, which is exactly the access pattern the server and
  runner need and exactly the one DuckDB refuses.

Keeping a writable table inside the DuckDB file would force the server to hold
the exclusive lock for the whole session and lock everyone else out of the
warehouse, so metadata must not migrate back.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "poc.duckdb"
META_PATH = PROJECT_ROOT / "data" / "poc_meta.sqlite"
TEMPLATES_DIR = PROJECT_ROOT / "templates"

# Fixed anchor date the synthetic data ends on. Kept in sync with data/seed.py.
ANCHOR_DATE = "2025-06-30"

_CONNECTIONS: dict[tuple[str, bool], duckdb.DuckDBPyConnection] = {}
_META_CONNECTIONS: dict[int, sqlite3.Connection] = {}

META_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_call_log (
  call_id         INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT,
  tool_name       TEXT,
  sql_text        TEXT,
  result_name     TEXT,
  row_count       INTEGER,
  called_at       TEXT
);
CREATE INDEX IF NOT EXISTS tool_call_log_conversation
  ON tool_call_log (conversation_id, call_id);

CREATE TABLE IF NOT EXISTS report_definitions (
  report_id          TEXT NOT NULL,
  definition_version INTEGER NOT NULL,
  report_name        TEXT,
  definition_json    TEXT,
  created_at         TEXT,
  parity_attempts    INTEGER,
  PRIMARY KEY (report_id, definition_version)
);
"""


def get_connection(read_only: bool = True) -> duckdb.DuckDBPyConnection:
    """Return a cached DuckDB connection to the analytic warehouse.

    Defaults to read-only, and every caller on the server and runner request
    paths should keep that default: a read-write open takes an exclusive
    OS-level lock that shuts every other process out of the file. Only
    ``data/seed.py`` opens the warehouse read-write, and it does so on its own
    connection while nothing else is attached.
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


def get_meta_connection() -> sqlite3.Connection:
    """Return a cached SQLite connection to the metadata store, creating it.

    The schema is applied with ``IF NOT EXISTS`` on every open, so the store
    bootstraps itself whether or not ``seed.py`` has run. WAL mode is what lets
    the runner read the registry while a server holds it open for writes;
    ``busy_timeout`` makes a concurrent writer wait rather than raise.

    Connections are cached per thread. FastMCP dispatches tool calls on worker
    threads, and a SQLite connection may not be shared across threads.
    """
    key = threading.get_ident()
    con = _META_CONNECTIONS.get(key)
    if con is None:
        META_PATH.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(META_PATH), timeout=30.0)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
        con.executescript(META_SCHEMA)
        con.commit()
        _META_CONNECTIONS[key] = con
    return con


def reset_meta_store() -> None:
    """Drop and recreate the metadata tables. Used by data/seed.py."""
    con = get_meta_connection()
    con.execute("DROP TABLE IF EXISTS tool_call_log")
    con.execute("DROP TABLE IF EXISTS report_definitions")
    con.executescript(META_SCHEMA)
    con.commit()


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
    """Execute a **DuckDB** query, inlining ``?`` placeholders as SQL literals.

    Works around a duckdb 1.5.4 deadlock: a ``?``-parameterized query
    (prepared statement) hangs indefinitely when executed inside the FastMCP
    server's tool-worker thread, while the identical query with literals runs
    fine. The bug does not reproduce in a plain interpreter, only under the MCP
    server's threaded execution, which is why every DuckDB read on the server
    request path must avoid bound parameters. Each ``?`` (there are none inside
    string literals in our internal SQL) is replaced positionally.

    This is a DuckDB workaround only -- do not route SQLite queries through it.
    ``sqlite3`` has no such deadlock, so the metadata store binds real
    parameters and keeps the safety that comes with them.
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
