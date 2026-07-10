"""End-to-end: capture the market story, then replay it as of an earlier date.

This is the acceptance test for the whole v2 stack. It drives the same in-process
session `scripts/demo_market_story.py` runs, saves a definition through the real
parity gate, and regenerates the report a quarter earlier -- which is the only way
to prove the definition is re-generatable rather than a frozen document.
"""

from __future__ import annotations

import json
import re

import pytest
from bs4 import BeautifulSoup

from runner import regenerate, render
from scripts import demo_market_story as demo
from scripts import market_queries as queries
from server import registry
from server.db import ANCHOR_DATE, get_connection, get_meta_connection

_EARLIER = "2025-03-31"

# Any absolute date left in the stored SQL means a window that will not move.
_DATE_LITERAL = re.compile(r"DATE\s+'\d{4}-\d{2}-\d{2}'")


@pytest.fixture(scope="module")
def saved():
    result = demo.main()
    assert result["status"] == "registered", result
    return result


@pytest.fixture(scope="module")
def definition(saved):
    return registry.get(
        get_meta_connection(), saved["report_id"], saved["definition_version"]
    )


def _islands(html: str) -> dict[str, list[dict]]:
    soup = BeautifulSoup(html, "html.parser")
    return {
        tag["data-result"]: json.loads(tag.string)
        for tag in soup.select('script[type="application/json"][data-result]')
    }


def test_registers_with_parity_passing_within_three_attempts(saved):
    assert saved["report_id"] == "las_vegas_race_to_1"
    assert saved["parity"]["passed"] is True
    assert saved["parity"]["attempts"] <= 3
    assert saved["unreplayable_sections"] == []


def test_stored_sql_is_quarter_grain_with_no_absolute_dates(definition):
    for query in definition["parameterized_sql"]:
        sql = query["sql"]
        assert "DATE_TRUNC('quarter', __REPORT_DATE__)" in sql, query["result_name"]
        leftovers = _DATE_LITERAL.findall(sql)
        assert leftovers == [], f"{query['result_name']} still pins {leftovers}"


def test_definition_carries_the_v2_furniture(definition):
    spec = definition["rendering_spec"]
    assert spec["layout"] == "tabbed-dashboard"
    assert spec["theme"] == "market-story-v1"
    assert spec["formats"] == ["html"]
    assert len(spec["sections"]) == 6
    assert len(spec["charts"]) >= 10
    assert len(spec["tables"]) == 4

    assert {b["block_id"] for b in definition["editorial_blocks"]} == {
        "thesis",
        "watchlist",
    }
    assert all(step.get("goal") for step in definition["reasoning_steps"])
    assert len(definition["reasoning_steps"]) == 3

    names = {q["result_name"] for q in definition["parameterized_sql"]}
    assert names == {name for name, _sql in queries.QUERIES}


def test_a_tabbed_layout_skips_markdown(definition):
    assert any("markdown output skipped" in w for w in definition["warnings"])


def _regenerate(tmp_path, as_of: str) -> str:
    outputs = regenerate.regenerate("las_vegas_race_to_1", None, as_of, tmp_path)
    assert len(outputs) == 1 and outputs[0].suffix == ".html"
    return outputs[0].read_text(encoding="utf-8")


def test_replay_shifts_the_window_and_recomputes_the_numbers(tmp_path, saved):
    at_anchor = _islands(_regenerate(tmp_path, ANCHOR_DATE))
    earlier = _islands(_regenerate(tmp_path, _EARLIER))

    # The race window slides back exactly one quarter.
    assert at_anchor["race_quarters"][0]["qtr"] == "Q2'21"
    assert earlier["race_quarters"][0]["qtr"] == "Q1'21"
    assert at_anchor["race_quarters"][-1]["qtr"] == "Q1'25"
    assert earlier["race_quarters"][-1]["qtr"] == "Q4'24"
    assert len(earlier["race_quarters"]) == 16  # still 16 complete quarters

    # And the KPIs move with it.
    assert at_anchor["kpi_summary"][0]["gap_now"] == 662
    assert earlier["kpi_summary"][0]["gap_now"] == 865


def test_editorial_blocks_replay_verbatim_and_keep_their_hash(tmp_path, definition):
    html = _regenerate(tmp_path, _EARLIER)
    for block in definition["editorial_blocks"]:
        assert len(block["html_sha256"]) == 64
    assert "Thesis: HCA can overtake UHS for #1." in html
    assert "Orthopedics is the one major service line" in html


def test_the_staleness_banner_tracks_the_watch_condition(tmp_path):
    """`gap_now < 800`: true at the anchor (662), false a quarter earlier (865)."""
    assert 'class="staleness-banner"' in _regenerate(tmp_path, ANCHOR_DATE)
    assert 'class="staleness-banner"' not in _regenerate(tmp_path, _EARLIER)


def test_output_carries_the_runtime_theme_and_one_panel_per_tab(tmp_path):
    html = _regenerate(tmp_path, _EARLIER)
    soup = BeautifulSoup(html, "html.parser")

    assert "charts_v1" in html and "function drawChart" in html  # runtime inlined
    assert ".staleness-banner {" in html  # theme CSS inlined
    assert len(soup.select('[role="tab"]')) == 6
    assert len(soup.select('[role="tabpanel"]')) == 6
    assert len(soup.select("[data-chart]")) == 12
    assert len(soup.select("table[data-result][data-columns]")) == 4
    assert len(soup.select('script[type="application/json"][data-result]')) == 9
    # The runtime fills the tables in the browser; the file ships them empty.
    assert soup.select_one("table[data-result] tbody").find("tr") is None


def test_narrative_is_recomputed_from_the_fresh_results(tmp_path, definition):
    """Reasoning prose is not parity-checked, so it must change with the data."""
    from server import parity, reasoning

    con = get_connection()
    engine = reasoning.HeuristicReasoningEngine()

    def narrate(as_of: str) -> dict:
        results = {
            q["result_name"]: parity.run_named_query(con, q["sql"], as_of)
            for q in definition["parameterized_sql"]
        }
        return engine.run(definition["reasoning_steps"], results)

    assert narrate(ANCHOR_DATE)["race_story"] != narrate(_EARLIER)["race_story"]


def test_kpi_labels_carry_no_frozen_period_literals(definition):
    """A hand-typed `Q1'25` in a heading would lie at every later replay."""
    assert not any("literal period label" in w for w in definition["warnings"])


def test_every_headline_number_is_bound_to_a_result(definition):
    soup = BeautifulSoup(definition["rendering_spec"]["template"], "html.parser")
    spans = soup.select("[data-value]")
    assert len(spans) >= 8
    for span in spans:
        assert "pick(" in span.get_text(), span["data-value"]


def test_the_runtime_derives_nothing():
    """Ground rule: SQL computes, JS draws. Guard the port against regressions.

    The reference artifact this runtime was ported from computed the gap, the
    share percentages, the year-over-year deltas, and the sort order in the
    browser. Those all moved into SQL; nothing may quietly move back.
    """
    source = (render.TEMPLATES_DIR / "runtime" / "charts_v1.js").read_text(
        encoding="utf-8"
    )
    code = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    code = re.sub(r"//.*", "", code)

    for forbidden in (".sort(", "* 100", "/ total"):
        assert forbidden not in code, f"{forbidden!r} looks like derivation in JS"
