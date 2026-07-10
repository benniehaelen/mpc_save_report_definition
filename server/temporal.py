"""Temporal re-parameterization: turn absolute date literals into expressions
relative to a report date token.

The definition stores SQL with the token ``__REPORT_DATE__``. Both the parity
gate and the runner substitute a real date at execution time. A literal such as
``DATE '2025-06-01'`` with anchor ``2025-06-30`` becomes
``__REPORT_DATE__ - INTERVAL 29 DAY``.

Quarter boundaries get their own grain. A quarter is not a fixed number of days,
so rewriting ``DATE '2021-04-01'`` as a day offset drifts across quarters of
unequal length as the report date moves. Such literals become
``DATE_TRUNC('quarter', __REPORT_DATE__) - INTERVAL 48 MONTH`` instead, which
lands on a quarter start for every report date.

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

# A confirmed quarter literal may reach ten years back. Beyond that it is far more
# likely to be an absolute reference date than a window bound.
_MAX_RELATIVE_QUARTERS = 40

_QUARTER_START_MONTHS = (1, 4, 7, 10)


def _dates_with_treatment(
    confirmations: list[dict] | None, treatment: str
) -> set[str]:
    """ISO dates the caller explicitly marked with `treatment`."""
    marked: set[str] = set()
    for entry in confirmations or []:
        if entry.get("treatment") != treatment:
            continue
        match = _ISO_DATE.search(entry.get("literal", ""))
        if match:
            marked.add(match.group(0))
    return marked


def _is_quarter_start(date: dt.date) -> bool:
    return date.month in _QUARTER_START_MONTHS and date.day == 1


def _quarter_start(date: dt.date) -> dt.date:
    return dt.date(date.year, (date.month - 1) // 3 * 3 + 1, 1)


def _quarters_between(literal: dt.date, anchor: dt.date) -> int:
    """Quarters from `literal` back to the quarter containing `anchor`.

    Measured against the anchor's quarter *start*, because that is what
    ``DATE_TRUNC('quarter', __REPORT_DATE__)`` evaluates to at replay time.
    """
    anchor_quarter = _quarter_start(anchor)
    return (anchor_quarter.year - literal.year) * 4 + (
        (anchor_quarter.month - 1) // 3 - (literal.month - 1) // 3
    )


def _quarter_expr(quarters_back: int) -> str:
    truncated = f"DATE_TRUNC('quarter', {REPORT_DATE_TOKEN})"
    if quarters_back == 0:
        return truncated
    if quarters_back > 0:
        return f"{truncated} - INTERVAL {3 * quarters_back} MONTH"
    return f"{truncated} + INTERVAL {3 * abs(quarters_back)} MONTH"


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

    Rules are tried in order, first match wins:

    1. the caller confirmed ``fixed``     -> leave it alone;
    2. the caller confirmed ``relative_quarter`` and it *is* a quarter start
       -> rewrite at quarter grain, up to 40 quarters back;
    3. unconfirmed quarter start within a year -> also quarter grain, because a
       day offset would drift off the boundary;
    4. otherwise -> the day-offset rewrite, or fixed-with-warning if too distant.

    ``re.sub`` never rescans what it substituted, so the ``'quarter'`` string this
    emits is not itself a candidate literal on some later pass. Neither is it one
    for ``_DATE_LITERAL``, which only matches ``'YYYY-MM-DD'``.
    """
    anchor = dt.date.fromisoformat(anchor_date)
    fixed = _dates_with_treatment(confirmations, "fixed")
    quarterly = _dates_with_treatment(confirmations, "relative_quarter")
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
        quarter_start = _is_quarter_start(literal_date)
        quarters_back = _quarters_between(literal_date, anchor) if quarter_start else 0

        if iso in quarterly:
            if not quarter_start:
                warnings.append(
                    f"Literal {iso} was confirmed relative_quarter but is not a "
                    "quarter start; falling back to a day offset."
                )
            elif abs(quarters_back) <= _MAX_RELATIVE_QUARTERS:
                return _quarter_expr(quarters_back)
            else:
                warnings.append(
                    f"Literal {iso} is {abs(quarters_back)} quarters from the "
                    "anchor; left fixed as it is likely an absolute reference date."
                )
                return match.group(0)
        elif quarter_start and quarters_back >= 1 and abs(delta) <= _MAX_RELATIVE_DAYS:
            # An unconfirmed *past* quarter boundary: a day offset would slide off
            # it as the report date moves through quarters of unequal length.
            # Current and future boundaries are left to the day-offset rule: an
            # exclusive upper bound is usually written as "the day after the end",
            # which the day form reproduces exactly.
            return _quarter_expr(quarters_back)

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
