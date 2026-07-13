"""Editorial blocks replay verbatim; their watch conditions flag them when stale."""

from __future__ import annotations

import hashlib

import pytest

from runner import regenerate, render
from server import compiler, parity
from server.db import ANCHOR_DATE, get_connection
from server.observability import RunRecorder

_CID = "editorial"
_PROSE = "<strong>Thesis: HCA can overtake UHS for #1.</strong>"

# The gap in the last complete quarter before the report date. At the anchor that
# is Q1'25 (gap ~662); at 2025-03-31 it is Q4'24 (gap ~865). The demo's watch
# threshold of 800 sits between them on purpose.
_KPI_SQL = (
    "SELECT SUM(cases) FILTER (WHERE health_system = 'Universal Health Services') "
    "       - SUM(cases) FILTER (WHERE is_hca) AS gap_now "
    "FROM marketshare_volume "
    "WHERE period_quarter = DATE_TRUNC('quarter', __REPORT_DATE__) - INTERVAL 3 MONTH"
)


@pytest.fixture(scope="module")
def con():
    return get_connection()


def _definition(watch: str | None) -> dict:
    html = render.build_island(
        "kpi_summary", {"columns": ["gap_now"], "rows": [{"gap_now": 662}]}
    ) + render.build_editorial_block("thesis", _PROSE, "2025-06-30", watch)
    log_rows = [{"result_name": "kpi_summary", "sql_text": _KPI_SQL}]
    return compiler.distill(
        "Race", [], {"content": html, "title": "Race"}, log_rows, ANCHOR_DATE
    )


def _stale(definition: dict, con, as_of: str) -> dict[str, str]:
    results = {
        q["result_name"]: parity.run_named_query(con, q["sql"], as_of)
        for q in definition["parameterized_sql"]
    }
    recorder = RunRecorder("race", 1, as_of)
    return regenerate._evaluate_watches(definition, results, recorder), recorder


def test_editorial_prose_replays_verbatim_and_its_hash_matches(con):
    definition = _definition("kpi_summary[0].gap_now < 800")
    block = definition["editorial_blocks"][0]

    template = definition["rendering_spec"]["template"]
    assert _PROSE in template  # untouched by the compiler

    results = {"kpi_summary": parity.run_named_query(con, _KPI_SQL, ANCHOR_DATE)}
    rendered = render.render_html(definition, results, {}, ANCHOR_DATE)
    assert _PROSE in rendered  # untouched by the renderer

    source = render.build_editorial_block(
        "thesis", _PROSE, "2025-06-30", "kpi_summary[0].gap_now < 800"
    )
    assert block["html_sha256"] == hashlib.sha256(source.encode()).hexdigest()


def test_watch_fires_at_the_anchor_and_not_a_quarter_earlier(con):
    """Seeded gap: ~662 at Q1'25 (fires), ~865 at Q4'24 (does not)."""
    definition = _definition("kpi_summary[0].gap_now < 800")

    fired, _recorder = _stale(definition, con, ANCHOR_DATE)
    assert "thesis" in fired
    assert "is now true" in fired["thesis"]
    assert "Authored 2025-06-30" in fired["thesis"]

    not_fired, _recorder = _stale(definition, con, "2025-03-31")
    assert not_fired == {}


def test_a_block_without_a_watch_is_never_flagged(con):
    definition = _definition(None)
    assert definition["editorial_blocks"][0]["watch"] is None
    stale, _recorder = _stale(definition, con, ANCHOR_DATE)
    assert stale == {}


def test_an_unresolvable_watch_degrades_to_a_banner_and_never_crashes(con):
    definition = _definition("kpi_summary[0].gap_now < 800")
    # The field vanishes from the fresh results, as if the query changed shape.
    definition["editorial_blocks"][0]["watch"]["field"] = "vanished"
    results = {"kpi_summary": parity.run_named_query(con, _KPI_SQL, ANCHOR_DATE)}
    recorder = RunRecorder("race", 1, ANCHOR_DATE)

    stale = regenerate._evaluate_watches(definition, results, recorder)
    assert "can no longer be evaluated" in stale["thesis"]
    assert any(s["attributes"].get("resolved") is False for s in recorder.spans)


def test_a_watch_on_a_missing_result_degrades_too(con):
    definition = _definition("kpi_summary[0].gap_now < 800")
    recorder = RunRecorder("race", 1, ANCHOR_DATE)
    stale = regenerate._evaluate_watches(definition, {}, recorder)
    assert "can no longer be evaluated" in stale["thesis"]


def test_every_watch_evaluation_records_a_span(con):
    definition = _definition("kpi_summary[0].gap_now < 800")
    _fired, recorder = _stale(definition, con, ANCHOR_DATE)
    watch_spans = [s for s in recorder.spans if s["name"] == "watch:thesis"]
    assert len(watch_spans) == 1
    assert watch_spans[0]["attributes"]["fired"] is True
    assert watch_spans[0]["attributes"]["resolved"] is True


def test_the_banner_appears_only_when_the_watch_fires(con):
    definition = _definition("kpi_summary[0].gap_now < 800")
    results = {"kpi_summary": parity.run_named_query(con, _KPI_SQL, ANCHOR_DATE)}

    without = render.render_html(definition, results, {}, ANCHOR_DATE, stale_blocks={})
    assert "staleness-banner" not in without

    with_banner = render.render_html(
        definition, results, {}, ANCHOR_DATE, stale_blocks={"thesis": "watch fired"}
    )
    assert 'class="staleness-banner"' in with_banner
    # The banner precedes the prose it qualifies, and the prose is still verbatim.
    assert with_banner.index("staleness-banner") < with_banner.index(_PROSE)


@pytest.mark.parametrize(
    "op,threshold,expected",
    [("<", 800, True), (">", 800, False), ("<=", 662, True), ("!=", 0, True), ("==", 0, False)],
)
def test_every_watch_operator_is_supported(con, op, threshold, expected):
    definition = _definition(f"kpi_summary[0].gap_now {op} {threshold}")
    stale, _recorder = _stale(definition, con, ANCHOR_DATE)
    assert ("thesis" in stale) is expected


def test_selector_literal_round_trips_what_the_compiler_stored():
    assert render.selector_literal(["index", -1]) == "[last]"
    assert render.selector_literal(["index", 3]) == "[3]"
    assert render.selector_literal(["match", "qtr", "Q1'25"]) == "[qtr='Q1''25']"
