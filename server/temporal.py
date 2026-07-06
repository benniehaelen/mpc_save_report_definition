"""Temporal re-parameterization: turn absolute date literals into expressions
relative to a report date token.

The definition stores SQL with the token ``__REPORT_DATE__``. Both the parity
gate and the runner substitute a real date at execution time. A literal such as
``DATE '2025-06-01'`` with anchor ``2025-06-30`` becomes
``__REPORT_DATE__ - INTERVAL 29 DAY``.

The transformation is deliberately conservative: if a literal cannot be
confidently expressed relative to the anchor, it is left fixed and a warning is
recorded. This is the riskiest piece of the real design, so it is heavily
unit tested.
"""

from __future__ import annotations

import datetime as dt
import re

REPORT_DATE_TOKEN = "__REPORT_DATE__"

# Matches an optional DATE keyword followed by a quoted ISO date, e.g.
# DATE '2025-06-01' or '2025-06-01'.
_DATE_LITERAL = re.compile(r"(?:DATE\s+)?'(\d{4}-\d{2}-\d{2})'", re.IGNORECASE)

# A bare ISO date, used to read the date out of a caller confirmation literal
# regardless of whether it was written with quotes or the DATE keyword.
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

# Beyond this distance from the anchor we cannot confidently treat a literal as
# a relative offset, so we leave it fixed and warn.
_MAX_RELATIVE_DAYS = 366


def _fixed_dates(confirmations: list[dict] | None) -> set[str]:
    """Extract the ISO dates the caller explicitly marked as fixed."""
    fixed: set[str] = set()
    for entry in confirmations or []:
        if entry.get("treatment") != "fixed":
            continue
        match = _ISO_DATE.search(entry.get("literal", ""))
        if match:
            fixed.add(match.group(0))
    return fixed


def _relative_expr(delta_days: int) -> str:
    if delta_days == 0:
        return REPORT_DATE_TOKEN
    if delta_days > 0:
        return f"{REPORT_DATE_TOKEN} + INTERVAL {delta_days} DAY"
    return f"{REPORT_DATE_TOKEN} - INTERVAL {abs(delta_days)} DAY"


def reparameterize(
    sql: str,
    anchor_date: str,
    confirmations: list[dict] | None = None,
) -> tuple[str, list[str]]:
    """Rewrite absolute date literals in ``sql`` relative to the anchor.

    Returns the rewritten SQL and a list of warnings for any literal left fixed.
    """
    anchor = dt.date.fromisoformat(anchor_date)
    fixed = _fixed_dates(confirmations)
    warnings: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        iso = match.group(1)
        if iso in fixed:
            warnings.append(f"Literal {iso} kept fixed by caller confirmation.")
            return match.group(0)
        try:
            literal_date = dt.date.fromisoformat(iso)
        except ValueError:
            warnings.append(f"Could not parse date literal {iso}; left fixed.")
            return match.group(0)
        delta = (literal_date - anchor).days
        if abs(delta) > _MAX_RELATIVE_DAYS:
            warnings.append(
                f"Literal {iso} is {abs(delta)} days from the anchor; "
                "left fixed as it is likely an absolute reference date."
            )
            return match.group(0)
        return _relative_expr(delta)

    return _DATE_LITERAL.sub(_replace, sql), warnings


def bind_report_date(sql: str, date_str: str) -> str:
    """Substitute a concrete date for the report-date token at execution time."""
    return sql.replace(REPORT_DATE_TOKEN, f"DATE '{date_str}'")
