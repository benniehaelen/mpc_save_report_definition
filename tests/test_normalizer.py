"""WS11-D: rewriting a free-form artifact into the v2 contract.

The normalizer's output is only useful if the *existing* pipeline accepts it, so
these tests run it through the real `artifact.detect_mode`, `artifact.parse`, and
`linter.lint` rather than asserting on markup shape alone.
"""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from server import artifact, call_log, extractor, fingerprint, linter, normalizer
from server.db import ANCHOR_DATE

_RACE_COLUMNS = ["qtr", "hca", "uhs", "gap"]
_RACE_ROWS = [
    {"qtr": "Q1", "hca": 10, "uhs": 30, "gap": 20},
    {"qtr": "Q2", "hca": 20, "uhs": 35, "gap": 15},
]
_KPI_COLUMNS = ["gap_now", "share_pct"]
_KPI_ROWS = [{"gap_now": 15, "share_pct": 36.4}]

_RACE_JS = "[{qtr: 'Q1', hca: 10, uhs: 30, gap: 20}, {qtr: 'Q2', hca: 20, uhs: 35, gap: 15}]"


def _call(name, columns, rows):
    fp, canonical = call_log.fingerprint_result(columns, rows)
    return {
        "result_name": name,
        "sql_text": f"SELECT * FROM {name}",
        "tool_name": "execute_sql",
        "result_fingerprint": fp,
        "result_rows": call_log.canonical_result(columns, rows) if canonical else None,
    }


@pytest.fixture
def calls():
    return [_call("race", _RACE_COLUMNS, _RACE_ROWS), _call("kpi", _KPI_COLUMNS, _KPI_ROWS)]


@pytest.fixture
def logged_rows():
    return {"race": _RACE_ROWS, "kpi": _KPI_ROWS}


_FREE_FORM = f"""<div class="page">
<style>.kpi {{ color: red; }}</style>
<div class="kpi"><span class="label">Share</span><strong>36.4%</strong></div>
<script>
const RACE = {_RACE_JS};
function draw() {{ RACE.forEach(function (d) {{ console.log(d.gap); }}); }}
</script>
<svg id="chart"></svg>
<p>HCA can overtake the market leader within four quarters if the current trend holds.</p>
</div>"""


def _normalize(html, calls, logged_rows, plan=None):
    report = fingerprint.match(html, calls)
    session = extractor.SessionContext("norm-test", calls, ANCHOR_DATE)
    if plan is None:
        plan = extractor.DeterministicExtractor().propose(html, report, session)
        plan, _ = extractor.validate_plan(plan, report, session)
    return normalizer.normalize(html, plan, logged_rows, report, save_date=ANCHOR_DATE)


def test_the_rewritten_artifact_is_a_v2_artifact(calls, logged_rows):
    html, _ = _normalize(_FREE_FORM, calls, logged_rows)
    assert artifact.detect_mode(_FREE_FORM) == "free_form"
    assert artifact.detect_mode(html) == "v2"


def test_the_rewritten_artifact_parses_with_no_problems(calls, logged_rows):
    html, _ = _normalize(_FREE_FORM, calls, logged_rows)
    model = artifact.parse(html)
    assert model.problems == []


def test_the_rewritten_artifact_passes_the_linter(calls, logged_rows):
    """In particular the injected helper must carry no Jinja delimiters."""
    html, _ = _normalize(_FREE_FORM, calls, logged_rows)
    unreplayable, _ = linter.lint(html)
    assert unreplayable == []


def test_the_island_helper_is_injected(calls, logged_rows):
    html, _ = _normalize(_FREE_FORM, calls, logged_rows)
    assert "function __ISLAND__(" in html
    assert 'data-island-helper="1"' in html


def test_the_helper_contains_no_template_delimiters():
    for token in ("{{", "}}", "{%", "%}"):
        assert token not in normalizer.ISLAND_HELPER


def test_the_helper_is_not_mistaken_for_a_data_island():
    model = artifact.parse(normalizer.ISLAND_HELPER)
    assert model.islands == {} and model.problems == []


def test_islands_carry_the_logged_rows_verbatim(calls, logged_rows):
    html, summary = _normalize(_FREE_FORM, calls, logged_rows)
    model = artifact.parse(html)
    assert model.islands["race"] == _RACE_ROWS
    assert summary.islands_written == 1


def test_a_projection_island_carries_the_full_result_rows(calls, logged_rows):
    """The blob showed two columns; the island must carry all four."""
    projected = "[{qtr: 'Q1', gap: 20}, {qtr: 'Q2', gap: 15}]"
    html = f"<div><script>const RACE = {projected};</script></div>"
    out, _ = _normalize(html, calls, logged_rows)
    assert artifact.parse(out).islands["race"] == _RACE_ROWS


def test_the_constant_is_repointed_at_the_island(calls, logged_rows):
    html, summary = _normalize(_FREE_FORM, calls, logged_rows)
    assert "const RACE = __ISLAND__('race');" in html
    assert summary.constants_repointed == 1
    assert _RACE_JS not in html  # the literal rows are gone from the script


def test_the_pages_own_drawing_code_survives(calls, logged_rows):
    html, _ = _normalize(_FREE_FORM, calls, logged_rows)
    assert "function draw()" in html
    assert "RACE.forEach" in html
    assert '<svg id="chart"></svg>' in html


def test_untouched_markup_stays_byte_identical(calls, logged_rows):
    html, _ = _normalize(_FREE_FORM, calls, logged_rows)
    assert "<style>.kpi { color: red; }</style>" in html
    assert '<span class="label">Share</span>' in html


def test_a_kpi_number_is_bound_with_its_inferred_filter(calls, logged_rows):
    html, summary = _normalize(_FREE_FORM, calls, logged_rows)
    soup = BeautifulSoup(html, "html.parser")
    span = soup.find("strong", attrs={"data-value": True})
    assert span["data-value"] == "kpi[0].share_pct | pct(1)"
    assert span.get_text() == "36.4%"  # display text untouched; parity strips the filter
    assert summary.values_bound == 1


def test_prose_becomes_an_editorial_block_dated_at_save_time(calls, logged_rows):
    html, summary = _normalize(_FREE_FORM, calls, logged_rows)
    model = artifact.parse(html)
    block = model.editorial_blocks[0]
    assert block["authored_as_of"] == ANCHOR_DATE
    assert "HCA can overtake" in block["html"]
    assert summary.editorial_blocks == 1


def test_a_number_inside_editorial_prose_is_not_also_bound(calls, logged_rows):
    """Editorial replays verbatim, so its numbers must not become data-value spans."""
    html = "<div><p>The gap now stands at 15 cases, the closest of the whole window.</p></div>"
    out, summary = _normalize(html, calls, logged_rows)
    assert summary.values_bound == 0
    assert artifact.parse(out).value_refs == []
    assert "15 cases" in out


def test_an_analytical_block_is_emptied_and_given_a_goal(calls, logged_rows):
    plan = {
        **extractor.empty_plan(),
        "narrative": [
            {"block_id": "b0", "tier": "analytical", "goal": "explain the race",
             "inputs": ["race"], "max_sentences": 2}
        ],
    }
    html, summary = _normalize(_FREE_FORM, calls, logged_rows, plan)
    model = artifact.parse(html)
    step = model.reasoning_steps[0]
    assert step["goal"] == "explain the race"
    assert step["inputs"] == [{"result_name": "race", "filter": None}]
    assert "HCA can overtake" not in html  # discarded; regenerated at replay
    assert summary.reasoning_blocks == 1


def test_a_computed_block_is_left_alone(calls, logged_rows):
    plan = {**extractor.empty_plan(), "narrative": [{"block_id": "b0", "tier": "computed"}]}
    html, summary = _normalize(_FREE_FORM, calls, logged_rows, plan)
    assert "HCA can overtake" in html
    assert summary.editorial_blocks == 0 and summary.reasoning_blocks == 0


def test_an_editorial_watch_is_emitted_and_parsed(calls, logged_rows):
    plan = {
        **extractor.empty_plan(),
        "narrative": [
            {"block_id": "b0", "tier": "editorial", "authored_as_of": ANCHOR_DATE,
             "watch": "kpi[0].gap_now < 800"}
        ],
    }
    html, _ = _normalize(_FREE_FORM, calls, logged_rows, plan)
    watch = artifact.parse(html).editorial_blocks[0]["watch"]
    assert watch["op"] == "<" and watch["value"] == 800.0


def test_tabs_are_emitted_when_proposed(calls, logged_rows):
    plan = {**extractor.empty_plan(), "tabs": [{"id": "story", "label": "Story"}]}
    html, summary = _normalize(_FREE_FORM, calls, logged_rows, plan)
    assert artifact.parse(html).tabs == [{"id": "story", "label": "Story"}]
    assert summary.tabs


def test_a_hand_written_table_becomes_a_bound_table(calls, logged_rows):
    rows = "".join(
        f"<tr><td>{r['qtr']}</td><td>{r['hca']}</td><td>{r['uhs']}</td><td>{r['gap']}</td></tr>"
        for r in _RACE_ROWS
    )
    html = (
        "<div><table><thead><tr><th>qtr</th><th>hca</th><th>uhs</th><th>gap</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
    )
    out, _ = _normalize(html, calls, logged_rows)
    model = artifact.parse(out)
    assert model.bound_tables[0]["result"] == "race"
    assert model.problems == []  # the tbody was emptied; the runtime fills it


def test_unmatched_blobs_are_reported_never_swallowed(calls, logged_rows):
    html = "<div><script>const MYSTERY = [{a: 1}, {a: 2}];</script></div>"
    _, summary = _normalize(html, calls, logged_rows)
    assert any("MYSTERY" in u for u in summary.unmatched)


def test_a_computed_constant_is_reported_as_computed_in_javascript(calls, logged_rows):
    html = "<div><script>const G = [{qtr: 'Q1', gap: d.uhs - d.hca}];</script></div>"
    _, summary = _normalize(html, calls, logged_rows)
    assert any("G is computed in JavaScript" in u for u in summary.unmatched)


def test_a_drawing_functions_locals_are_not_reported(calls, logged_rows):
    html = "<div><script>var w = 720; var svg = document.getElementById('c');</script></div>"
    _, summary = _normalize(html, calls, logged_rows)
    assert summary.unmatched == []


def test_an_ambiguous_number_is_reported(calls, logged_rows):
    html = "<div><b>15</b></div>"  # kpi.gap_now and race[1].gap both equal 15
    _, summary = _normalize(html, calls, logged_rows)
    assert any("ambiguous number" in u for u in summary.unmatched)


# ---------------------------------------------------------------------------
# The new lint rule
# ---------------------------------------------------------------------------


def test_editorial_on_a_table_row_is_unreplayable():
    """The staleness banner is a <div>; it cannot precede a <tr>."""
    html = '<table><tbody><tr data-editorial="note">frozen</tr></tbody></table>'
    unreplayable, _ = linter.lint(html)
    assert any("cannot precede a table row" in u for u in unreplayable)


@pytest.mark.parametrize("tag", ["tr", "td", "th", "thead", "tbody"])
def test_editorial_on_any_table_internal_element_is_rejected(tag):
    html = f'<table><{tag} data-editorial="note">x</{tag}></table>'
    unreplayable, _ = linter.lint(html)
    assert unreplayable


def test_editorial_on_a_div_inside_a_cell_is_fine():
    html = '<table><tbody><tr><td><div data-editorial="note">ok</div></td></tr></tbody></table>'
    unreplayable, _ = linter.lint(html)
    assert unreplayable == []


def test_the_existing_lint_rules_still_fire():
    assert linter.lint("<p>{{ danger }}</p>")[0]
    assert linter.lint("<h2>Gap to #1 (Q1'25)</h2>")[1]
