"""Tool-call log keyed by conversation_id, plus the result-fingerprint canon.

Only execute_sql writes here: the log is the lineage record that lets the
compiler recover which named results a report was built from. Dry runs are not
lineage and never touch this table.

Lives in the SQLite metadata store (`server.db.META_PATH`), not the DuckDB
warehouse, so that writing lineage never takes an exclusive lock on the analytic
tables. See the module docstring in `server/db.py`.

## Fingerprints

Every logged call also stores a **fingerprint** of what the query returned, plus
(under a size cap) the canonical rows themselves. Save-time structure extraction
matches numbers on a free-form page against this evidence rather than re-running
the query, because a save-time re-execution reproducing the session's rows is
true in this static POC and false against a live warehouse.

The canonicalization is not free-floating -- it is pinned to the parity gate:

* **Typed values** (result rows, and the values a JS literal reader recovers) use
  ``canonical_scalar(value)``, which mirrors ``parity._coerce``. That equivalence
  is what makes a fingerprint match *imply* the island survives parity, which is
  the whole "fingerprint first, parity last" contract. It is also the only one of
  parity's three normalizers that preserves booleans, and ``is_hca`` is a real
  boolean column.
* **Display text** (HTML cells, scalar candidates) uses
  ``canonical_scalar(text, from_text=True)``, which mirrors ``parity._normalize_value``
  and undoes the display filters: ``+1,234`` -> ``1234.0``, ``34.5%`` -> ``34.5``,
  ``-2.7pp`` -> ``-2.7``.

Both land on the same scalar set (float rounded to 6 / str / bool / None), so a JS
literal ``1234``, an HTML cell ``"1,234"``, and a DuckDB ``int 1234`` hash equal.
If these drift from `server/parity.py`, the extraction gate becomes unsound.
"""

from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import json
import re
import sqlite3

# Beyond this many bytes of canonical JSON we store the fingerprint alone. The
# row cap in tools.execute_sql is 500, so only very wide results reach this.
FINGERPRINT_ROW_CAP = 256 * 1024

_TAG_RE = re.compile(r"<[^>]+>")


def canonical_scalar(value: object, *, from_text: bool = False) -> object:
    """Normalize one value to the canonical scalar set.

    ``from_text=True`` mirrors ``parity._normalize_value`` (undo display filters);
    otherwise mirrors ``parity._coerce`` (typed values, booleans preserved).
    """
    if from_text:
        stripped = _TAG_RE.sub("", str(value)).strip()
        bare = stripped
        if bare.endswith("pp"):
            bare = bare[:-2]
        elif bare.endswith("%"):
            bare = bare[:-1]
        bare = bare.lstrip("+").replace(",", "")
        try:
            return round(float(bare), 6)
        except ValueError:
            return stripped

    # Booleans are ints in Python; guard them first or True collapses into 1.0.
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, decimal.Decimal):
        return round(float(value), 6)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, (int, float)):
        return round(float(value), 6)
    try:
        return round(float(str(value).replace(",", "")), 6)
    except ValueError:
        return str(value).strip()


def canonical_result(columns: list[str], rows: list[dict]) -> dict:
    """Canonicalize a query result to ``{"columns": [...], "rows": [[...], ...]}``.

    Rows keep their returned order -- a reordered result is a different result,
    because parity compares islands positionally.
    """
    cols = list(columns)
    return {
        "columns": cols,
        "rows": [[canonical_scalar(row.get(col)) for col in cols] for row in rows],
    }


def canonical_json(canonical: dict) -> str:
    """Deterministic JSON for hashing. Key order is the column order, not sorted."""
    return json.dumps(canonical, separators=(",", ":"), ensure_ascii=False)


def fingerprint_canonical(canonical: dict) -> str:
    return hashlib.sha256(canonical_json(canonical).encode("utf-8")).hexdigest()


def fingerprint_result(
    columns: list[str], rows: list[dict]
) -> tuple[str, str | None]:
    """Return ``(fingerprint, canonical_json)``; the JSON is None beyond the cap."""
    canonical = canonical_result(columns, rows)
    blob = canonical_json(canonical)
    fingerprint = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    if len(blob.encode("utf-8")) > FINGERPRINT_ROW_CAP:
        return fingerprint, None
    return fingerprint, blob


def log_call(
    con: sqlite3.Connection,
    conversation_id: str,
    tool_name: str,
    sql_text: str,
    result_name: str,
    row_count: int,
    result_fingerprint: str | None = None,
    result_rows: str | None = None,
) -> int:
    """Append a row to tool_call_log and return the assigned call_id."""
    cur = con.execute(
        "INSERT INTO tool_call_log "
        "(conversation_id, tool_name, sql_text, result_name, row_count, called_at, "
        " result_fingerprint, result_rows) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            conversation_id,
            tool_name,
            sql_text,
            result_name,
            row_count,
            dt.datetime.now().isoformat(sep=" ", timespec="seconds"),
            result_fingerprint,
            result_rows,
        ),
    )
    con.commit()
    return cur.lastrowid


def fetch(con: sqlite3.Connection, conversation_id: str) -> list[dict]:
    """Return this conversation's logged calls, oldest first.

    ``result_rows`` comes back parsed into ``{"columns", "rows"}`` (or None when
    the result exceeded the cap, or the row predates fingerprinting). Structure
    extraction reads it as ground truth, so parsing here keeps every consumer
    from re-implementing it.
    """
    cur = con.execute(
        """
        SELECT call_id, conversation_id, tool_name, sql_text, result_name,
               row_count, called_at, result_fingerprint, result_rows
        FROM tool_call_log
        WHERE conversation_id = ?
        ORDER BY call_id
        """,
        (conversation_id,),
    )
    cols = [d[0] for d in cur.description]
    calls = [dict(zip(cols, row)) for row in cur.fetchall()]
    for call in calls:
        raw = call.get("result_rows")
        if raw:
            try:
                call["result_rows"] = json.loads(raw)
            except json.JSONDecodeError:
                call["result_rows"] = None
        else:
            call["result_rows"] = None
    return calls
