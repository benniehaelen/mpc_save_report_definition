"""The parity gate.

check(definition, final_artifact, as_of_date) executes every named query in the
definition with the report-date token bound to the given date, renders the
definition's template with those results, then compares extracted data content
(not markup) against the original artifact. Only table cell values and
data-value numbers are compared, so markup differences never cause a mismatch.

A v2 artifact is compared island by island instead: same row count, then every
field of every row. `data-value` texts are compared after undoing the display
filters (commas, a leading '+', a trailing '%' or 'pp'), so `+1,234` matches the
raw 1234 that SQL returned.

v2 adds one class of failure the v1 gate had no equivalent for. Charts, bound
tables, reasoning inputs, and editorial watches all *reference* a result and a
field by name, and none of them is exercised by rendering: the runtime fills them
in a browser the gate never opens. A chart pointing at a column that no longer
exists would sail through a value comparison and render an empty box at replay.
So references are resolved explicitly, and a broken one blocks the save.
"""

from __future__ import annotations

import json
import re

import duckdb
from bs4 import BeautifulSoup

from runner import render
from server import artifact, reasoning, temporal

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
        for q in definition["parameterized_sql"]
    }
    narratives = reasoning.get_engine().run(
        definition.get("reasoning_steps", []), results_by_name
    )
    original = final_artifact.get("content", "")
    try:
        rendered = render.render_html(definition, results_by_name, narratives, as_of_date)
    except render.SelectorError as exc:
        # A selector that no longer resolves is a broken report, not a near miss.
        return {"passed": False, "diff_summary": str(exc)}

    if artifact.is_v2(original):
        return _check_v2(definition, original, rendered, results_by_name)
    return _check_legacy(original, rendered)


def _check_legacy(original: str, rendered: str) -> dict:
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


# ---------------------------------------------------------------------------
# v2: islands, filter-aware value comparison, and reference completeness.
# ---------------------------------------------------------------------------

def _normalize_value(text: str) -> object:
    """Undo the display filters, so `+1,234` compares equal to the raw 1234.

    Applied to both sides, so it can never make a real mismatch look equal.
    """
    stripped = _TAG_RE.sub("", text).strip()
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


def _coerce(value: object) -> object:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return round(float(value), 6)
    try:
        return round(float(str(value).replace(",", "")), 6)
    except ValueError:
        return str(value).strip()


def _extract_islands(html: str) -> dict[str, list[dict]]:
    soup = BeautifulSoup(html, "html.parser")
    islands: dict[str, list[dict]] = {}
    for tag in soup.select('script[type="application/json"][data-result]'):
        try:
            islands[tag["data-result"]] = json.loads(tag.string or "[]")
        except (json.JSONDecodeError, TypeError):
            islands[tag["data-result"]] = []
    return islands


def _extract_value_texts(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    return [tag.get_text() for tag in soup.select("[data-value]")]


def _diff_islands(expected: dict, actual: dict) -> str | None:
    for name, expected_rows in expected.items():
        actual_rows = actual.get(name)
        if actual_rows is None:
            return f"island '{name}' is missing from the regenerated report"
        if len(expected_rows) != len(actual_rows):
            return (
                f"island '{name}' has {len(actual_rows)} rows, "
                f"expected {len(expected_rows)}"
            )
        for index, (expected_row, actual_row) in enumerate(
            zip(expected_rows, actual_rows)
        ):
            for field, expected_value in expected_row.items():
                if field not in actual_row:
                    return f"island '{name}' row {index} is missing field '{field}'"
                if _coerce(expected_value) != _coerce(actual_row[field]):
                    return (
                        f"island '{name}' row {index} field '{field}': "
                        f"expected {expected_value!r}, got {actual_row[field]!r}"
                    )
    return None


def _check_references(definition: dict, results_by_name: dict) -> str | None:
    """Every declared reference must name a live result and a real field.

    Rendering does not exercise these -- the runtime resolves them in a browser --
    so without this a chart could point at a dropped column and still pass.
    """
    spec = definition.get("rendering_spec", {})

    def fields_of(result_name: str) -> set[str] | None:
        result = results_by_name.get(result_name)
        return set(result["columns"]) if result else None

    def check_one(kind: str, result_name: str, wanted: list[str]) -> str | None:
        columns = fields_of(result_name)
        if columns is None:
            return f"{kind} references result '{result_name}', which has no query"
        for field in wanted:
            if field and field not in columns:
                return (
                    f"{kind} references field '{field}' of '{result_name}', "
                    f"which the query does not return"
                )
        return None

    for chart in spec.get("charts") or []:
        wanted = [chart.get("x"), chart.get("label_field"), chart.get("value_field")]
        wanted += [s.get("field") for s in chart.get("series") or []]
        wanted += list((chart.get("filter") or {}).keys())
        if chart.get("display_field"):
            wanted.append(chart["display_field"])
        problem = check_one(
            f"chart '{chart.get('id') or chart.get('type')}'", chart.get("result", ""), wanted
        )
        if problem:
            return problem

    for table in spec.get("tables") or []:
        problem = check_one(
            f"table '{table['result']}'",
            table["result"],
            [column["field"] for column in table["columns"]],
        )
        if problem:
            return problem

    for step in definition.get("reasoning_steps") or []:
        for inp in step.get("inputs") or []:
            wanted = [inp["filter"]["col"]] if inp.get("filter") else []
            problem = check_one(
                f"reasoning '{step['step_id']}'", inp["result_name"], wanted
            )
            if problem:
                return problem

    for block in definition.get("editorial_blocks") or []:
        watch = block.get("watch")
        if not watch:
            continue
        problem = check_one(
            f"editorial '{block['block_id']}' watch", watch["result"], [watch["field"]]
        )
        if problem:
            return problem
    return None


def _check_v2(
    definition: dict, original: str, rendered: str, results_by_name: dict
) -> dict:
    island_diff = _diff_islands(_extract_islands(original), _extract_islands(rendered))
    if island_diff:
        return {"passed": False, "diff_summary": island_diff}

    expected = [_normalize_value(text) for text in _extract_value_texts(original)]
    actual = [_normalize_value(text) for text in _extract_value_texts(rendered)]
    if len(expected) != len(actual):
        return {
            "passed": False,
            "diff_summary": (
                f"found {len(actual)} data-value numbers, expected {len(expected)}"
            ),
        }
    for index, (want, got) in enumerate(zip(expected, actual)):
        if want != got:
            return {
                "passed": False,
                "diff_summary": f"value {index}: expected {want!r}, got {got!r}",
            }

    reference_problem = _check_references(definition, results_by_name)
    if reference_problem:
        return {"passed": False, "diff_summary": reference_problem}

    return {"passed": True, "diff_summary": "all islands and values match"}
