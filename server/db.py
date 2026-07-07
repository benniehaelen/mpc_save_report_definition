"""Shared database path, connection helpers, and project-wide constants.

A single small module so every other module agrees on where the DuckDB file
lives and what the fixed anchor date is. Connections are cached per
(path, read_only) pair so the server reuses one read-write connection.
"""

from __future__ import annotations

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
