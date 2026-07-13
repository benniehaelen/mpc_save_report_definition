"""Cache a proposed normalization plan between the two save calls.

`save_report_definition` returns a plan for confirmation on the first call and
must apply exactly that plan on the second. Caching it serves two purposes:

* The LLM is **not re-prompted** on the second call, so the plan the client
  confirmed is the plan that runs. Re-proposing could quietly return something
  else.
* The token is a hash of the plan, so a page edited between the two calls
  produces a different token and the stale confirmation is refused rather than
  applied to markup nobody looked at.

A token is **single-use**, and it names a *proposal*, not a plan. Two saves of the
same unchanged page produce the same plan, so a plan-content hash could not tell
them apart; the token therefore mixes in the conversation and the moment the
proposal was made. Once a proposal has registered a definition its row is marked
consumed, and presenting the token again is refused with the report it already
produced -- rather than silently registering a second version of the same report.

Consumed rows are kept as an audit trail of what a human actually approved;
*unconsumed* proposals for a conversation are pruned when a new one supersedes
them, so the table cannot grow without bound.

The cached row also carries a hash of the HTML the proposal was made against. A
plan is a set of source offsets into that HTML, so applying it to an edited page
would splice at the wrong bytes. The second call refuses when the page has moved.

Lives in the SQLite metadata store; see `server/db.py`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3


def _now() -> str:
    return dt.datetime.now().isoformat(sep=" ", timespec="microseconds")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def plan_token(plan: dict) -> str:
    """A stable sha256 over the plan's content alone."""
    return _sha256(json.dumps(plan, sort_keys=True, separators=(",", ":"), default=str))


def html_token(html: str) -> str:
    """A sha256 of the artifact the plan's source offsets point into."""
    return _sha256(html)


def new_token(plan: dict, conversation_id: str) -> tuple[str, str]:
    """Mint a token for one proposal. Returns ``(token, created_at)``.

    Distinct proposals of an identical plan get distinct tokens, which is what
    makes "already used" a meaningful answer.
    """
    created_at = _now()
    return _sha256(f"{plan_token(plan)}|{conversation_id}|{created_at}"), created_at


def save(
    con: sqlite3.Connection,
    token: str,
    conversation_id: str,
    plan: dict,
    html_sha256: str,
    created_at: str,
) -> None:
    """Cache a proposal, dropping this conversation's earlier unconsumed ones.

    A superseded proposal can never be confirmed -- the client only ever holds the
    newest token -- so keeping it would leak a row for every re-save of a page.
    Consumed rows are never pruned: they are the record of what was approved.
    """
    con.execute(
        "DELETE FROM extraction_plans "
        "WHERE conversation_id = ? AND consumed_at IS NULL AND token <> ?",
        (conversation_id, token),
    )
    payload = {"plan": plan, "html_sha256": html_sha256}
    con.execute(
        "INSERT OR IGNORE INTO extraction_plans "
        "(token, conversation_id, plan_json, created_at, consumed_at, report_id) "
        "VALUES (?, ?, ?, ?, NULL, NULL)",
        (token, conversation_id, json.dumps(payload, default=str), created_at),
    )
    con.commit()


def load(con: sqlite3.Connection, token: str) -> dict | None:
    """Return the cached entry, or None for an unknown token."""
    row = con.execute(
        "SELECT conversation_id, plan_json, created_at, consumed_at, report_id "
        "FROM extraction_plans WHERE token = ?",
        (token,),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row[1])
    except json.JSONDecodeError:
        return None
    return {
        "conversation_id": row[0],
        "plan": payload.get("plan", {}),
        "html_sha256": payload.get("html_sha256"),
        "created_at": row[2],
        "consumed_at": row[3],
        "report_id": row[4],
    }


def mark_consumed(con: sqlite3.Connection, token: str, report_id: str) -> None:
    """Record that this plan registered a definition. The token is now spent."""
    con.execute(
        "UPDATE extraction_plans SET consumed_at = ?, report_id = ? WHERE token = ?",
        (_now(), report_id, token),
    )
    con.commit()
