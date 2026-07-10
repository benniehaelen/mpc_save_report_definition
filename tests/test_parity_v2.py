"""Tests for the v2 parity gate: islands, filtered values, reference completeness."""

from __future__ import annotations

import pytest

from runner import render
from server import call_log, compiler, parity
from server.db import ANCHOR_DATE, get_connection, get_meta_connection

_CID = "parity-v2"

_RACE_SQL = (
    "SELECT 'Q' || quarter(period_quarter) || '''' "
    "|| strftime(period_quarter, '%y') AS qtr, "
    "SUM(cases) FILTER (WHERE is_hca) AS hca_cases, "
    "SUM(cases) FILTER (WHERE health_system = 'Universal Health Services') "
    "  - SUM(cases) FILTER (WHERE is_hca) AS gap "
    "FROM marketshare_volume "
    "WHERE period_quarter >= DATE '2024-04-01' "
    "  AND period_quarter < DATE '2025-04-01' "
    "GROUP BY period_quarter ORDER BY period_quarter"
)


@pytest.fixture(scope="module")
def con():
    return get_connection()


@pytest.fixture(scope="module")
def race(con):
    return parity.run_named_query(con, _RACE_SQL, ANCHOR_DATE)


@pytest.fixture(scope="module")
def log_rows(con):
    meta = get_meta_connection()
    call_log.log_call(meta, _CID, "execute_sql", _RACE_SQL, "race", 4)
    return call_log.fetch(meta, _CID)


def _distill(html: str, log_rows) -> dict:
    return compiler.distill(
        "Race", [], {"content": html, "title": "Race"}, log_rows, ANCHOR_DATE
    )


def _artifact(race: dict, extra: str = "") -> str:
    return render.build_island("race", race) + extra


def test_matching_islands_pass(con, race, log_rows):
    html = _artifact(race)
    result = parity.check(con, _distill(html, log_rows), {"content": html}, ANCHOR_DATE)
    assert result["passed"], result["diff_summary"]


def test_an_edited_cell_inside_an_island_fails_and_names_island_row_field(
    con, race, log_rows
):
    html = _artifact(race)
    definition = _distill(html, log_rows)
    # Corrupt one number in the *original* artifact only.
    tampered = html.replace(str(race["rows"][1]["gap"]), "999999", 1)
    result = parity.check(con, definition, {"content": tampered}, ANCHOR_DATE)
    assert not result["passed"]
    assert "island 'race'" in result["diff_summary"]
    assert "row 1" in result["diff_summary"]
    assert "field 'gap'" in result["diff_summary"]


def test_a_signed_comma_formatted_span_passes_against_its_raw_number(
    con, race, log_rows
):
    """`+1,234` and 1234 are the same number wearing different clothes."""
    gap = race["rows"][-1]["gap"]
    span = render.build_value_span_v2(
        "race[last].gap | signed | thousands", f"+{gap:,}"
    )
    html = _artifact(race, span)
    result = parity.check(con, _distill(html, log_rows), {"content": html}, ANCHOR_DATE)
    assert result["passed"], result["diff_summary"]


def test_a_percent_and_pp_suffixed_span_passes(con, race, log_rows):
    hca = race["rows"][0]["hca_cases"]
    html = _artifact(
        race,
        render.build_value_span_v2("race[first].hca_cases | pct(1)", f"{hca:.1f}%")
        + render.build_value_span_v2("race[first].hca_cases | pp", f"{hca}pp"),
    )
    result = parity.check(con, _distill(html, log_rows), {"content": html}, ANCHOR_DATE)
    assert result["passed"], result["diff_summary"]


def test_a_wrong_headline_number_still_fails(con, race, log_rows):
    span = render.build_value_span_v2("race[last].gap | thousands", "1,234")
    html = _artifact(race, span)
    result = parity.check(con, _distill(html, log_rows), {"content": html}, ANCHOR_DATE)
    assert not result["passed"]
    assert "value 0" in result["diff_summary"]


def test_a_chart_referencing_a_nonexistent_field_fails_and_names_it(
    con, race, log_rows
):
    """The runtime resolves charts in a browser the gate never opens, so a stale
    field reference has to be caught here or nowhere."""
    chart = render.build_chart_div(
        {
            "type": "line",
            "result": "race",
            "x": "qtr",
            "series": [{"field": "no_such_column"}],
        },
        "c1",
    )
    html = _artifact(race, chart)
    result = parity.check(con, _distill(html, log_rows), {"content": html}, ANCHOR_DATE)
    assert not result["passed"]
    assert "no_such_column" in result["diff_summary"]


def test_a_bound_table_referencing_a_nonexistent_field_fails(con, race, log_rows):
    table = render.build_bound_table("race", [{"field": "ghost", "header": "Ghost"}])
    html = _artifact(race, table)
    result = parity.check(con, _distill(html, log_rows), {"content": html}, ANCHOR_DATE)
    assert not result["passed"]
    assert "ghost" in result["diff_summary"]


def test_an_editorial_watch_on_a_nonexistent_field_fails(con, race, log_rows):
    block = render.build_editorial_block(
        "thesis", "<strong>T</strong>", "2025-06-30", "race[last].vanished < 800"
    )
    html = _artifact(race, block)
    result = parity.check(con, _distill(html, log_rows), {"content": html}, ANCHOR_DATE)
    assert not result["passed"]
    assert "vanished" in result["diff_summary"]


def test_a_reasoning_filter_on_a_nonexistent_column_fails(con, race, log_rows):
    block = render.build_reasoning_block("s1", "Explain", ["race[missing='x']"])
    html = _artifact(race, block)
    result = parity.check(con, _distill(html, log_rows), {"content": html}, ANCHOR_DATE)
    assert not result["passed"]
    assert "missing" in result["diff_summary"]


def test_valid_chart_table_reasoning_and_watch_all_pass(con, race, log_rows):
    html = _artifact(
        race,
        render.build_chart_div(
            {
                "type": "line",
                "result": "race",
                "x": "qtr",
                "series": [{"field": "gap"}],
                "filter": {"qtr": "Q1'25"},
            },
            "c1",
        )
        + render.build_bound_table(
            "race",
            [
                {"field": "qtr", "header": "Quarter"},
                {"field": "gap", "header": "Gap", "filters": [["thousands", []]]},
            ],
        )
        + render.build_reasoning_block("s1", "Explain the gap", ["race"])
        + render.build_editorial_block(
            "thesis", "<strong>T</strong>", "2025-06-30", "race[last].gap < 800"
        ),
    )
    result = parity.check(con, _distill(html, log_rows), {"content": html}, ANCHOR_DATE)
    assert result["passed"], result["diff_summary"]


def test_a_selector_that_no_longer_resolves_fails_cleanly(con, race, log_rows):
    span = render.build_value_span_v2("race[qtr='Q9''99'].gap", "0")
    html = _artifact(race, span)
    result = parity.check(con, _distill(html, log_rows), {"content": html}, ANCHOR_DATE)
    assert not result["passed"]
    assert "no row where qtr" in result["diff_summary"]


def test_editorial_prose_and_reasoning_text_are_excluded_from_comparison(
    con, race, log_rows
):
    """Prose is not data; it must never be able to fail the gate."""
    html = _artifact(
        race,
        render.build_reasoning_block("s1", "Explain", ["race"])
        + render.build_editorial_block(
            "thesis", "<p>The gap is 4,199 cases.</p>", "2025-06-30"
        ),
    )
    definition = _distill(html, log_rows)
    result = parity.check(con, definition, {"content": html}, ANCHOR_DATE)
    assert result["passed"], result["diff_summary"]


def test_bound_tables_do_not_disturb_the_legacy_table_extraction(race):
    """A bound table's empty tbody yields zero cells on both sides."""
    html = render.build_bound_table("race", [{"field": "qtr", "header": "Quarter"}])
    assert parity._extract_tables(html) == {"race": []}
