"""Cache a proposed normalization plan between the two save calls.

`save_report_definition` returns a plan for confirmation on the first call and
must apply exactly that plan on the second. Caching it serves two purposes:

* The LLM is **not re-prompted** on the second call, so the plan the client
  confirmed is the plan that runs. Re-proposing could quietly return something
  else.
* The token is a hash of the plan, so a page edited between the two calls
  produces a different token and the stale confirmation is refused rather than
  applied to markup nobody looked at.

Lives in the SQLite metadata store; see `server/db.py`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3


def plan_token(plan: dict) -> str:
    """A stable sha256 over the plan's content."""
    canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def save(con: sqlite3.Connection, token: str, conversation_id: str, plan: dict) -> None:
    con.execute(
        "INSERT OR REPLACE INTO extraction_plans "
        "(token, conversation_id, plan_json, created_at) VALUES (?, ?, ?, ?)",
        (
            token,
            conversation_id,
            json.dumps(plan, default=str),
            dt.datetime.now().isoformat(sep=" ", timespec="seconds"),
        ),
    )
    con.commit()


def load(con: sqlite3.Connection, token: str) -> dict | None:
    """Return ``{conversation_id, plan, created_at}`` or None for an unknown token."""
    row = con.execute(
        "SELECT conversation_id, plan_json, created_at FROM extraction_plans WHERE token = ?",
        (token,),
    ).fetchone()
    if row is None:
        return None
    try:
        plan = json.loads(row[1])
    except json.JSONDecodeError:
        return None
    return {"conversation_id": row[0], "plan": plan, "created_at": row[2]}


def delete(con: sqlite3.Connection, token: str) -> None:
    con.execute("DELETE FROM extraction_plans WHERE token = ?", (token,))
    con.commit()
