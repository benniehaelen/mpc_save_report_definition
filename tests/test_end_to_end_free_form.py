"""WS11-F: a contract-free page saves, replays, and shifts with the report date.

Runs the real `scripts/demo_free_form.py` capture path (deterministic, offline),
then replays the registered definition a quarter earlier and checks that the data
moved while the structure held.
"""

from __future__ import annotations

import json
import sys

import pytest
from bs4 import BeautifulSoup

from runner import regenerate
from scripts import demo_free_form as demo
from server import artifact, registry
from server.db import ANCHOR_DATE, get_meta_connection

_EARLIER = "2025-03-31"


@pytest.fixture(scope="module")
def saved():
    """The deterministic path: every number fingerprints, so one call registers."""
    result = demo.main()
    assert result["status"] == "registered", result
    return result


@pytest.fixture(scope="module")
def definition(saved):
    return registry.get(get_meta_connection(), saved["report_id"], saved["definition_version"])


def _islands(html: str) -> dict[str, list[dict]]:
    soup = BeautifulSoup(html, "html.parser")
    return {
        tag["data-result"]: json.loads(tag.string)
        for tag in soup.select('script[type="application/json"][data-result]')
    }


def _regenerate(tmp_path, as_of: str) -> str:
    outputs = regenerate.regenerate(
        "las_vegas_free_form", None, as_of, tmp_path, ["html"]
    )
    return next(p for p in outputs if p.suffix == ".html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The save
# ---------------------------------------------------------------------------


def test_the_submitted_page_carried_no_contract_markup():
    """If the demo page ever gains a data-* attribute, this test stops proving anything."""
    html = demo._page(
        [{"qtr": "Q1", "hca": 1, "uhs": 2, "gap": 1, "gap_trend": 0}],
        [{"esl": "ORTHO", "qtr": "Q1", "share_pct": 30.0}],
        [{"qtr": "Q1", "gap_pct": 50.0}],
        {"esl_gainers": 12, "esl_total": 17, "hca_share_pct": 35.3},
    )
    assert artifact.detect_mode(html) == "free_form"
    assert "data-value" not in html and "data-editorial" not in html


def test_a_free_form_page_registers_in_one_call(saved):
    assert saved["status"] == "registered"
    assert saved["parity"]["passed"]
    assert saved["parity"]["attempts"] == 1


def test_the_definition_holds_the_queries_the_page_actually_used(definition):
    names = {q["result_name"] for q in definition["parameterized_sql"]}
    assert {"race_quarters", "esl_quarters", "race_gap_pct", "kpi_summary"} <= names


def test_every_query_was_reparameterized_to_the_report_date(definition):
    for query in definition["parameterized_sql"]:
        assert "__REPORT_DATE__" in query["sql"], query["result_name"]


def test_the_derived_gap_query_carries_quarter_tokens(definition):
    """A quarter boundary must become DATE_TRUNC, not a day offset that drifts."""
    sql = next(
        q["sql"] for q in definition["parameterized_sql"] if q["result_name"] == "race_gap_pct"
    )
    assert "DATE_TRUNC('quarter', __REPORT_DATE__)" in sql


def test_the_island_helper_is_in_the_stored_template(definition):
    assert "function __ISLAND__(" in definition["rendering_spec"]["template"]


def test_the_pages_own_drawing_code_is_in_the_stored_template(definition):
    template = definition["rendering_spec"]["template"]
    assert "function drawRace(" in template
    assert "const RACE = __ISLAND__('race_quarters');" in template


def test_the_prose_became_editorial_blocks(definition):
    assert len(definition["editorial_blocks"]) == 3
    for block in definition["editorial_blocks"]:
        assert block["authored_as_of"] == ANCHOR_DATE
        assert len(block["html_sha256"]) == 64


def test_nothing_was_silently_dropped(definition, saved):
    assert definition["unreplayable_sections"] == []
    # The ambiguous KPI number is reported rather than guessed at.
    assert any("ambiguous number" in w for w in saved["warnings"])


# ---------------------------------------------------------------------------
# The replay
# ---------------------------------------------------------------------------


def test_the_islands_shift_with_the_report_date(saved, tmp_path):
    at_anchor = _islands(_regenerate(tmp_path, ANCHOR_DATE))
    earlier = _islands(_regenerate(tmp_path, _EARLIER))

    anchor_race = at_anchor["race_quarters"]
    earlier_race = earlier["race_quarters"]
    assert anchor_race[-1]["qtr"] != earlier_race[-1]["qtr"]
    assert anchor_race[-1]["gap"] != earlier_race[-1]["gap"]
    # The window keeps its length; only its position moves.
    assert len(anchor_race) == len(earlier_race)


def test_the_derived_query_replays_too(saved, tmp_path):
    earlier = _islands(_regenerate(tmp_path, _EARLIER))
    assert "race_gap_pct" in earlier
    assert len(earlier["race_gap_pct"]) == len(earlier["race_quarters"])


def test_the_structure_holds_across_the_shift(saved, tmp_path):
    anchor_html = _regenerate(tmp_path, ANCHOR_DATE)
    earlier_html = _regenerate(tmp_path, _EARLIER)
    for html in (anchor_html, earlier_html):
        soup = BeautifulSoup(html, "html.parser")
        assert len(_islands(html)) == 3
        assert len(soup.select("[data-value]")) == 2
        assert "function __ISLAND__(" in html
        assert "function drawRace(" in html


def test_bound_numbers_are_re_evaluated_not_frozen(saved, tmp_path):
    """The two unique KPI numbers render from fresh results through `pick`."""
    html = _regenerate(tmp_path, ANCHOR_DATE)
    soup = BeautifulSoup(html, "html.parser")
    values = [span.get_text(strip=True) for span in soup.select("[data-value]")]
    assert values == ["12", "17"]


def test_editorial_prose_replays_verbatim(saved, tmp_path):
    earlier_html = _regenerate(tmp_path, _EARLIER)
    assert "Thesis: HCA can take the market outright within four quarters" in earlier_html
    assert "Orthopedics is the exception that deserves attention" in earlier_html


def test_a_frozen_number_inside_editorial_prose_is_not_bound(saved, tmp_path):
    """Editorial replays verbatim; its numbers must not have become data-value spans."""
    html = _regenerate(tmp_path, ANCHOR_DATE)
    soup = BeautifulSoup(html, "html.parser")
    for block in soup.select("[data-editorial]"):
        assert block.select("[data-value]") == []


# ---------------------------------------------------------------------------
# The LLM path degrades to the deterministic one, with no network
# ---------------------------------------------------------------------------


def test_the_llm_path_falls_back_when_the_sdk_is_unavailable(monkeypatch):
    """Mirrors the convention in test_bindings_reasoning: force the import to fail."""
    from server import extractor, fingerprint

    monkeypatch.setenv("POC_DISTILLER", "anthropic")
    monkeypatch.setitem(sys.modules, "anthropic", None)

    engine = extractor.get_extractor()
    assert isinstance(engine, extractor.AnthropicExtractor)

    html = "<div><p>Prose long enough to count as a narrative block for the extractor.</p></div>"
    report = fingerprint.match(html, [])
    session = extractor.SessionContext("llm-fallback", [], ANCHOR_DATE)
    plan = engine.propose(html, report, session)

    assert not extractor.has_inferences(plan)
    assert plan["narrative"][0]["tier"] == "editorial"
