"""The parity gate.

check(definition, final_artifact, as_of_date) executes every named query in the
definition with the report-date token bound to the given date, renders the
definition's template with those results, then compares extracted data content
(not markup) against the original artifact. Only table cell values and
data-value numbers are compared, so markup differences never cause a mismatch.
"""

from __future__ import annotations

import re

import duckdb

from runner import render
from server import temporal

_TABLE_RE = re.compile(
    r'<table\b[^>]*\bdata-result="([^"]+)"[^>]*>(.*?)</table>',
    re.IGNORECASE | re.DOTALL,
)
_TD_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_VALUE_RE = re.compile(
    r'<(\w+)\b[^>]*\bdata-value="([^"]+)"[^>]*>(.*?)</\1>',
    re.IGNORECASE | re.DOTALL,
)


def run_named_query(
    con: duckdb.DuckDBPyConnection, sql_token: str, date_str: str
) -> dict:
    """Execute a tokenized query with the report date bound, capped at 500 rows."""
    sql = temporal.bind_report_date(sql_token, date_str)
    con.execute(f"SELECT * FROM ({sql}) AS _q LIMIT 500")
    columns = [d[0] for d in con.description]
    rows = [dict(zip(columns, row)) for row in con.fetchall()]
    return {"columns": columns, "rows": rows}


def _normalize(text: str) -> object:
    stripped = _TAG_RE.sub("", text).strip()
    try:
        return round(float(stripped.replace(",", "")), 6)
    except ValueError:
        return stripped


def _extract_tables(html: str) -> dict[str, list[object]]:
    tables: dict[str, list[object]] = {}
    for name, inner in _TABLE_RE.findall(html):
        tables[name] = [_normalize(cell) for cell in _TD_RE.findall(inner)]
    return tables


def _extract_values(html: str) -> list[object]:
    return [_normalize(text) for _tag, _ref, text in _VALUE_RE.findall(html)]


def _diff_tables(
    expected: dict[str, list[object]], actual: dict[str, list[object]]
) -> str | None:
    for name, exp_cells in expected.items():
        act_cells = actual.get(name)
        if act_cells is None:
            return f"table '{name}' is missing from the regenerated report"
        if len(exp_cells) != len(act_cells):
            return (
                f"table '{name}' has {len(act_cells)} cells, "
                f"expected {len(exp_cells)}"
            )
        for i, (exp, act) in enumerate(zip(exp_cells, act_cells)):
            if exp != act:
                return (
                    f"table '{name}' cell {i}: expected {exp!r}, got {act!r}"
                )
    return None


def check(
    con: duckdb.DuckDBPyConnection,
    definition: dict,
    final_artifact: dict,
    as_of_date: str,
) -> dict:
    """Return {passed, diff_summary}. as_of_date should be the anchor for save."""
    results_by_name = {
        q["result_name"]: run_named_query(con, q["sql"], as_of_date)
        for q in definition["queries"]
    }
    rendered = render.render_definition(definition, results_by_name)
    original = final_artifact.get("content", "")

    exp_tables = _extract_tables(original)
    act_tables = _extract_tables(rendered)
    table_diff = _diff_tables(exp_tables, act_tables)
    if table_diff:
        return {"passed": False, "diff_summary": table_diff}

    exp_values = _extract_values(original)
    act_values = _extract_values(rendered)
    if len(exp_values) != len(act_values):
        return {
            "passed": False,
            "diff_summary": (
                f"found {len(act_values)} data-value numbers, "
                f"expected {len(exp_values)}"
            ),
        }
    for i, (exp, act) in enumerate(zip(exp_values, act_values)):
        if exp != act:
            return {
                "passed": False,
                "diff_summary": f"value {i}: expected {exp!r}, got {act!r}",
            }

    return {"passed": True, "diff_summary": "all extracted values match"}
