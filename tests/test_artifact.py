"""Tests for the v2 artifact parser (server/artifact.py)."""

from __future__ import annotations

import pytest

from runner import render
from server import artifact


def test_selector_grammar_round_trips():
    cases = {
        "r.f": ("index", 0),
        "r[0].f": ("index", 0),
        "r[3].f": ("index", 3),
        "r[first].f": ("index", 0),
        "r[last].f": ("index", -1),
        "r[col='val'].f": ("match", "col", "val"),
    }
    for raw, expected in cases.items():
        result, selector, field, filters = artifact.parse_value_ref(raw)
        assert (result, selector, field, filters) == ("r", expected, "f", ())


def test_selector_escapes_single_quote_by_doubling():
    _r, selector, _f, _filters = artifact.parse_value_ref("r[qtr='Q1''25'].gap")
    assert selector == ("match", "qtr", "Q1'25")


def test_filter_chain_parses_every_whitelisted_filter():
    _r, _s, _f, filters = artifact.parse_value_ref(
        "r.f | thousands | signed | pct(1) | pp | round(2)"
    )
    assert filters == (
        ("thousands", ()),
        ("signed", ()),
        ("pct", (1,)),
        ("pp", ()),
        ("round", (2,)),
    )


@pytest.mark.parametrize(
    "raw",
    [
        "r.f | shout",  # not whitelisted
        "r.f | pct",  # missing required argument
        "r.f | thousands(2)",  # takes no argument
        "r[bogus].f",  # unrecognized selector
        "rf",  # no field
    ],
)
def test_bad_value_grammar_raises(raw):
    with pytest.raises(artifact.GrammarError):
        artifact.parse_value_ref(raw)


def test_islands_accept_both_shapes():
    html = (
        '<script type="application/json" data-result="a">[{"x":1}]</script>'
        '<script type="application/json" data-result="b">'
        '{"columns":["x"],"rows":[{"x":2}]}</script>'
    )
    model = artifact.parse(html)
    assert model.problems == []
    assert model.islands == {"a": [{"x": 1}], "b": [{"x": 2}]}


def test_malformed_island_filter_and_chart_land_in_problems():
    html = (
        '<script type="application/json" data-result="a">{nope}</script>'
        '<span data-value="a.f | shout">1</span>'
        "<div data-chart='{\"type\":\"pie\",\"result\":\"a\"}'></div>"
    )
    problems = artifact.parse(html).problems
    assert len(problems) == 3
    assert any("island 'a'" in p for p in problems)
    assert any("shout" in p for p in problems)
    assert any("pie" in p for p in problems)


def test_chart_json_attribute_may_contain_a_greater_than_sign():
    """A naive scan to the next '>' would truncate the tag and lose the chart."""
    html = (
        '<div data-chart=\'{"type":"line","result":"r","x":"q",'
        '"series":[{"field":"gap","label":"Gap > 0"}]}\'></div>'
    )
    model = artifact.parse(html)
    assert model.problems == []
    assert model.charts[0]["series"][0]["label"] == "Gap > 0"


def test_bound_table_requires_empty_tbody():
    populated = (
        '<table data-result="a" data-columns="f:H">'
        "<tbody><tr><td>1</td></tr></tbody></table>"
    )
    assert "empty <tbody>" in artifact.parse(populated).problems[0]

    empty = '<table data-result="a" data-columns="f:H|thousands|style:sign"><tbody></tbody></table>'
    model = artifact.parse(empty)
    assert model.problems == []
    assert model.bound_tables[0]["columns"] == [
        {"field": "f", "header": "H", "filters": [["thousands", []]], "style": "sign"}
    ]


def test_reasoning_v1_and_v2_are_separated():
    html = (
        '<p data-reasoning="v1" data-over="t.f" data-agg="max"></p>'
        '<p data-reasoning="v2" data-goal="Explain" data-inputs="t, t[c=\'x\']"></p>'
    )
    model = artifact.parse(html)
    assert model.problems == []
    assert [s["step_id"] for s in model.legacy_reasoning] == ["v1"]
    step = model.reasoning_steps[0]
    assert step["step_id"] == "v2"
    assert step["max_sentences"] == 3  # default
    assert step["inputs"] == [
        {"result_name": "t", "filter": None},
        {"result_name": "t", "filter": {"col": "c", "val": "x"}},
    ]


def test_editorial_block_captures_exact_source_and_watch():
    html = '<div data-editorial="t" data-authored-as-of="2025-06-30" data-watch="k[0].gap < 800">A <b>b</b> c</div>'
    block = artifact.parse(html).editorial_blocks[0]
    assert block["html"] == html  # byte-exact, nested markup intact
    assert block["authored_as_of"] == "2025-06-30"
    assert block["watch"]["op"] == "<"
    assert block["watch"]["value"] == 800.0
    assert block["watch"]["ref"].result == "k"


def test_outer_span_is_byte_exact_for_nested_same_name_tags():
    html = '<div id="outer"><div id="inner">x</div>tail</div>'
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    outer = soup.find("div")
    span = artifact.outer_span(html, outer, artifact.line_starts(html))
    assert html[span.outer_start : span.outer_end] == html


def test_referenced_names_covers_every_declaration_kind():
    html = (
        '<script type="application/json" data-result="island">[{"f":1}]</script>'
        '<span data-value="vals.f">1</span>'
        "<div data-chart='{\"type\":\"bar\",\"result\":\"chart_src\","
        '"label_field":"f","value_field":"f"}\'></div>'
        '<table data-result="table_src" data-columns="f:H"><tbody></tbody></table>'
        '<p data-reasoning="s" data-goal="g" data-inputs="reason_src"></p>'
        '<div data-editorial="e" data-watch="watch_src[0].f < 1">x</div>'
    )
    model = artifact.parse(html)
    assert model.problems == []
    assert set(model.referenced_names()) == {
        "island",
        "vals",
        "chart_src",
        "table_src",
        "reason_src",
        "watch_src",
    }


def test_legacy_artifact_is_not_detected_as_v2():
    """The v1 data-value regex mis-parses v2 grammar, so the boundary must hold."""
    legacy = (
        "<h1>Division Admissions and Census</h1>"
        + render.build_table_html(
            "admissions_by_division",
            ["division", "admissions"],
            [{"division": "North", "admissions": 5}],
        )
        + "<p class='headline'>"
        + render.build_value_span("overall_occupancy", "occupancy_rate", 0.71)
        + "</p>"
        + render.build_reasoning_para(
            "occ_summary", "census_by_facility", "occupancy_rate", "max"
        )
    )
    assert artifact.is_v2(legacy) is False


@pytest.mark.parametrize(
    "html",
    [
        '<script type="application/json" data-result="a">[]</script>',
        '<div data-editorial="e">x</div>',
        "<div data-chart='{}'></div>",
        '<table data-result="a" data-columns="f:H"><tbody></tbody></table>',
        '<p data-reasoning="s" data-goal="g" data-inputs="a"></p>',
        '<nav data-tabs=\'[{"id":"a","label":"A"}]\'></nav>',
        '<span data-value="a[last].f">1</span>',
        '<span data-value="a.f | thousands">1</span>',
    ],
)
def test_each_v2_marker_is_detected(html):
    assert artifact.is_v2(html) is True


def test_real_las_vegas_snippet_parses():
    """The reference report's constants, converted to islands, parse cleanly."""
    html = """
<div class="kpi-strip">
  <div class="kpi-card hero">
    <div class="kpi-label">Gap to #1</div>
    <div class="value" data-value="kpi_summary[0].gap_now | thousands">731</div>
  </div>
</div>
<nav data-tabs='[{"id":"story","label":"Executive Story"},{"id":"race","label":"The Race to #1"}]'></nav>
<script type="application/json" data-result="race_quarters">
[{"qtr":"Q1'22","hca":19168,"uhs":23367,"gap":4199,"gap_trend":0},
 {"qtr":"Q2'22","hca":19035,"uhs":23447,"gap":4412,"gap_trend":213}]
</script>
<script type="application/json" data-result="esl_share_change">
[{"esl":"GENERAL SURGERY","share_change_pp":1.9,"vol_change":294},
 {"esl":"ORTHOPEDICS","share_change_pp":-2.7,"vol_change":-167}]
</script>
<div id="raceChart" data-chart='{"type":"line","result":"race_quarters","x":"qtr",
  "series":[{"field":"uhs","label":"UHS","color":"#092240"},
            {"field":"hca","label":"HCA","color":"#E75925"}],"width":700,"height":300}'></div>
<div id="eslShareChart" data-chart='{"type":"diverging_bar","result":"esl_share_change",
  "label_field":"esl","value_field":"share_change_pp","pos_color":"#E75925","neg_color":"#092240"}'></div>
<table data-result="race_quarters" data-columns="qtr:Quarter, hca:HCA|thousands, uhs:UHS|thousands, gap:Gap|thousands, gap_trend:Trend|signed|thousands|style:sign">
  <thead><tr><th>Quarter</th><th>HCA</th><th>UHS</th><th>Gap</th><th>Trend</th></tr></thead>
  <tbody></tbody>
</table>
<p data-reasoning="race_story" data-goal="Explain how the gap to the market leader moved."
   data-inputs="race_quarters" data-max-sentences="3"></p>
<div data-editorial="thesis" data-authored-as-of="2025-06-30"
     data-watch="kpi_summary[0].gap_now < 800">
  <strong>Thesis: HCA can overtake UHS for #1.</strong>
</div>
"""
    model = artifact.parse(html)
    assert model.problems == []
    assert set(model.islands) == {"race_quarters", "esl_share_change"}
    assert len(model.islands["race_quarters"]) == 2
    assert [c["type"] for c in model.charts] == ["line", "diverging_bar"]
    assert model.bound_tables[0]["columns"][-1]["style"] == "sign"
    assert model.tabs == [
        {"id": "story", "label": "Executive Story"},
        {"id": "race", "label": "The Race to #1"},
    ]
    assert model.editorial_blocks[0]["watch"]["value"] == 800.0
    assert model.reasoning_steps[0]["max_sentences"] == 3
