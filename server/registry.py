"""Definition registry backed by the report_definitions table.

A report_id is a slug derived from the report name; saving the same name again
creates a new definition_version. The full definition document is stored as JSON.

Lives in the SQLite metadata store (`server.db.META_PATH`), not the DuckDB
warehouse, so the runner can read a definition while a server is writing one.
See the module docstring in `server/db.py`.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "report"


def register(
    con: sqlite3.Connection,
    report_name: str,
    definition: dict,
    parity_attempts: int,
) -> tuple[str, int]:
    """Insert a new definition version and return (report_id, version).

    The version is derived from the current maximum, so the read and the insert
    run inside one ``BEGIN IMMEDIATE`` transaction: the metadata store now
    admits concurrent writers, and two servers saving the same report name would
    otherwise both compute the same next version and collide on the primary key.
    """
    report_id = slugify(report_name)
    try:
        con.execute("BEGIN IMMEDIATE")
        version = con.execute(
            "SELECT COALESCE(MAX(definition_version), 0) + 1 "
            "FROM report_definitions WHERE report_id = ?",
            (report_id,),
        ).fetchone()[0]
        definition = {
            **definition,
            "report_id": report_id,
            "definition_version": version,
        }
        con.execute(
            "INSERT INTO report_definitions VALUES (?, ?, ?, ?, ?, ?)",
            (
                report_id,
                version,
                report_name,
                json.dumps(definition),
                dt.datetime.now().isoformat(sep=" ", timespec="seconds"),
                parity_attempts,
            ),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    return report_id, version


def get(
    con: sqlite3.Connection,
    report_id: str,
    version: int | None = None,
) -> dict:
    """Fetch a stored definition document, defaulting to the latest version."""
    if version is None:
        row = con.execute(
            "SELECT definition_json FROM report_definitions "
            "WHERE report_id = ? ORDER BY definition_version DESC LIMIT 1",
            (report_id,),
        ).fetchone()
    else:
        row = con.execute(
            "SELECT definition_json FROM report_definitions "
            "WHERE report_id = ? AND definition_version = ?",
            (report_id, version),
        ).fetchone()
    if row is None:
        raise KeyError(f"No definition for report_id={report_id!r} version={version}")
    return json.loads(row[0])


def list_all(con: sqlite3.Connection) -> list[dict]:
    """Return every registered report_id/version with its name and timestamp."""
    cur = con.execute(
        """
        SELECT report_id, definition_version, report_name, created_at, parity_attempts
        FROM report_definitions
        ORDER BY report_id, definition_version
        """
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]
