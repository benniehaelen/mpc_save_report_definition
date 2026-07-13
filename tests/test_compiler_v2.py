"""Tests for compiler v2 and the artifact linter."""

from __future__ import annotations

from runner import render
from server import compiler, linter

_RACE = {
    "columns": ["qtr", "gap", "gap_trend"],
    "rows": [
        {"qtr": "Q1'24", "gap": 1393, "gap_trend": 0},
        {"qtr": "Q1'25", "gap": 662, "gap_trend": -203},
    ],
}
_ESL = {
    "columns": ["esl", "share_change_pp"],
    "rows": [{"esl": "ORTHOPEDICS", "share_change_pp": -2.7}],
}

_LOG = [
    {"result_name": "race", "sql_text": "SELECT * FROM t WHERE d >= DATE '2021-04-01'"},
    {"result_name": "esl", "sql_text": "SELECT * FROM e"},
    {"result_name": "scratch", "sql_text": "SELECT COUNT(*) FROM t"},
]


def _distill(html: str, artifact_extra: dict | None = None, **kwargs) -> dict:
    final_artifact = {"content": html, "title": "Race", "formats": ["html"]}
    final_artifact.update(artifact_extra or {})
    return compiler.distill(
        "Race", [], final_artifact, _LOG, "2025-06-30", **kwargs
    )


def _two_island_artifact() -> str:
    return (
        render.build_island("race", _RACE)
        + render.build_island("esl", _ESL)
        + render.build_value_span_v2("race[last].gap | thousands", "662")
    )


def test_two_island_artifact_distills_to_tojson_and_pick():
    definition = _distill(_two_island_artifact())
    template = definition["rendering_spec"]["template"]
    assert "{{ race.rows | tojson }}" in template
    assert "{{ esl.rows | tojson }}" in template
    assert "{{ pick(race, '[last]', 'gap') | thousands }}" in template
    assert definition["unreplayable_sections"] == []


def test_dead_end_queries_drop_out():
    definition = _distill(_two_island_artifact())
    names = [q["result_name"] for q in definition["parameterized_sql"]]
    assert names == ["race", "esl"]
    assert "scratch" not in names


def test_unmatched_reference_is_unreplayable():
    html = render.build_island("nope", _RACE)
    definition = _distill(html)
    assert any(
        "reference 'nope' has no matching query" in s
        for s in definition["unreplayable_sections"]
    )


def test_quarter_confirmation_reaches_the_stored_sql():
    definition = _distill(
        _two_island_artifact(),
        temporal_confirmations=[
            {"literal": "2021-04-01", "treatment": "relative_quarter"}
        ],
    )
    sql = definition["parameterized_sql"][0]["sql"]
    assert "DATE_TRUNC('quarter', __REPORT_DATE__) - INTERVAL 48 MONTH" in sql
    assert "2021-04-01" not in sql


def test_sign_style_merges_into_an_existing_class():
    html = render.build_island("race", _RACE) + (
        '<span class="value" data-value="race[last].gap_trend | signed" '
        'data-style="sign">-203</span>'
    )
    template = _distill(html)["rendering_spec"]["template"]
    assert 'class="value {{ sign_class(pick(race, \'[last]\', \'gap_trend\')) }}"' in template


def test_sign_style_adds_a_class_when_there_is_none():
    html = render.build_island("race", _RACE) + (
        '<span data-value="race[last].gap_trend" data-style="sign">-203</span>'
    )
    template = _distill(html)["rendering_spec"]["template"]
    assert "<span class=\"{{ sign_class(pick(race, '[last]', 'gap_trend')) }}\"" in template


def test_match_selector_survives_quoting_into_the_template():
    html = render.build_island("race", _RACE) + render.build_value_span_v2(
        "race[qtr='Q1''25'].gap", "662"
    )
    definition = _distill(html)
    assert definition["unreplayable_sections"] == []
    rendered = render.render_html(definition, {"race": _RACE})
    assert ">662<" in rendered


def test_editorial_block_is_verbatim_hashed_and_gets_a_banner_slot():
    block = render.build_editorial_block(
        "thesis", "<strong>Thesis</strong>", "2025-06-30", "race[last].gap < 800"
    )
    html = render.build_island("race", _RACE) + block
    definition = _distill(html)
    template = definition["rendering_spec"]["template"]

    assert "{{ editorial_banner('thesis') }}" in template
    # The prose itself is untouched, and the banner precedes it.
    assert template.index("editorial_banner") < template.index("<strong>Thesis</strong>")
    assert "<strong>Thesis</strong>" in template

    stored = definition["editorial_blocks"][0]
    assert stored["block_id"] == "thesis"
    assert stored["authored_as_of"] == "2025-06-30"
    assert stored["watch"] == {
        "raw": "race[last].gap < 800",
        "result": "race",
        "selector": ["index", -1],
        "field": "gap",
        "op": "<",
        "value": 800.0,
    }
    assert len(stored["html_sha256"]) == 64


def test_reasoning_v2_step_is_collected_and_templated():
    html = render.build_island("race", _RACE) + render.build_reasoning_block(
        "s1", "Explain the gap", ["race"], max_sentences=2
    )
    definition = _distill(html)
    assert "{{ reasoning['s1'] }}" in definition["rendering_spec"]["template"]
    assert definition["reasoning_steps"] == [
        {
            "step_id": "s1",
            "goal": "Explain the gap",
            "inputs": [{"result_name": "race", "filter": None}],
            "max_sentences": 2,
            "style": None,
        }
    ]


def test_charts_tables_and_sections_land_in_the_rendering_spec():
    html = (
        render.build_island("race", _RACE)
        + render.build_chart_div(
            {"type": "line", "result": "race", "x": "qtr", "series": [{"field": "gap"}]},
            "c1",
        )
        + render.build_bound_table("race", [{"field": "qtr", "header": "Quarter"}])
        + render.build_tabs([{"id": "a", "label": "A"}])
    )
    spec = _distill(html)["rendering_spec"]
    assert spec["charts"][0]["type"] == "line"
    assert spec["tables"][0]["result"] == "race"
    assert spec["sections"] == [{"id": "a", "label": "A"}]
    # Chart and table markup is left alone; the runtime fills it.
    assert "data-chart=" in spec["template"]
    assert "<tbody></tbody>" in spec["template"]


def test_tabbed_layout_forces_html_only_and_warns():
    definition = _distill(
        _two_island_artifact(),
        {"formats": ["html", "md"], "layout": "tabbed-dashboard", "theme": "market-story-v1"},
    )
    spec = definition["rendering_spec"]
    assert spec["formats"] == ["html"]
    assert spec["layout"] == "tabbed-dashboard"
    assert spec["theme"] == "market-story-v1"
    assert any("markdown output skipped" in w for w in definition["warnings"])


def test_metric_bindings_come_from_island_columns_not_table_headers():
    catalog = {
        "metrics": {"gap", "share_change_pp"},
        "value_sets": {"esl_level_2": {"ORTHOPEDICS"}},
        "dimensions": {"esl": "esl_level_2"},
    }
    html = (
        render.build_island("race", _RACE)
        + render.build_island("esl", _ESL)
        # A display label that is not a field name; it must not bind to anything.
        + render.build_bound_table("race", [{"field": "gap", "header": "Gap to #1"}])
    )
    bindings = _distill(html, catalog=catalog)["metric_bindings"]
    by_field = {(b["result_name"], b["field"]): b for b in bindings}
    assert by_field[("race", "gap")]["metric_id"] == "gap"
    assert by_field[("esl", "esl")]["value_set"] == "esl_level_2"
    assert by_field[("esl", "share_change_pp")]["metric_id"] == "share_change_pp"
    assert not any(b["field"] == "Gap to #1" for b in bindings)


def test_artifact_problems_surface_as_unreplayable_sections():
    html = (
        render.build_island("race", _RACE)
        + '<span data-value="race.gap | shout">1</span>'
        + "<div data-chart='{\"type\":\"pie\",\"result\":\"race\"}'></div>"
    )
    sections = _distill(html)["unreplayable_sections"]
    assert any("shout" in s for s in sections)
    assert any("pie" in s for s in sections)


# --- linter ---------------------------------------------------------------


def test_linter_rejects_author_written_template_syntax():
    unreplayable, _warnings = linter.lint("<p>{{ race.rows[0].gap }}</p>")
    assert any("template delimiters" in s for s in unreplayable)


def test_linter_flags_a_literal_quarter_in_a_heading():
    _unreplayable, warnings = linter.lint("<h2>Gap to #1 (Q1'25)</h2>")
    assert len(warnings) == 1
    assert "Q1'25" in warnings[0]


def test_linter_flags_a_literal_quarter_in_a_kpi_label():
    _unreplayable, warnings = linter.lint('<div class="kpi-label">Gap (Q4\'24)</div>')
    assert len(warnings) == 1


def test_linter_allows_a_quarter_bound_to_a_result_inside_a_heading():
    html = "<h2>Gap to #1 <span data-value=\"race[last].qtr\">Q1'25</span></h2>"
    _unreplayable, warnings = linter.lint(html)
    assert warnings == []


def test_linter_ignores_editorial_prose_and_chart_declarations():
    html = (
        "<div data-editorial=\"t\"><h3>Since Q1'22 the gap narrowed</h3></div>"
        "<div data-chart='{\"type\":\"line\",\"result\":\"r\",\"title\":\"Q1&#39;25\"}'></div>"
        "<p>Plain prose mentioning Q1'25 is not a heading.</p>"
    )
    _unreplayable, warnings = linter.lint(html)
    assert warnings == []


def test_lint_warning_does_not_block_the_save():
    """Literal labels warn; only parity blocks registration."""
    html = "<h2>Gap (Q1'25)</h2>" + _two_island_artifact()
    definition = _distill(html)
    assert any("literal period label" in w for w in definition["warnings"])
    assert definition["unreplayable_sections"] == []


# --- the legacy boundary --------------------------------------------------


def _legacy_artifact() -> str:
    return (
        "<h1>Division Admissions and Census</h1>"
        + render.build_table_html(
            "race", ["qtr", "gap"], [{"qtr": "Q1'24", "gap": 1393}]
        )
        + "<p class='headline'>"
        + render.build_value_span("esl", "share_change_pp", -2.7)
        + "</p>"
        + render.build_reasoning_para("occ", "race", "gap", "max")
    )


def test_legacy_artifact_takes_the_legacy_path_unchanged():
    html = _legacy_artifact()
    from server import artifact

    assert artifact.is_v2(html) is False

    definition = compiler.distill(
        "Legacy", [], {"content": html, "formats": ["html", "md"]}, _LOG, "2025-06-30"
    )
    template = definition["rendering_spec"]["template"]
    # v1 shapes, not v2 ones.
    assert "{% for __row in race.rows %}" in template
    assert "{{ esl.rows[0]['share_change_pp'] }}" in template
    assert "pick(" not in template and "tojson" not in template
    assert "editorial_banner" not in template
    # v1 keeps both formats and grows no v2 keys.
    assert definition["rendering_spec"]["formats"] == ["html", "md"]
    assert "editorial_blocks" not in definition
    assert "charts" not in definition["rendering_spec"]
    assert definition["reasoning_steps"] == [
        {"step_id": "occ", "result_name": "race", "field": "gap", "agg": "max"}
    ]


def test_legacy_and_v2_distillers_are_selected_by_the_sniffer(monkeypatch):
    calls = []
    monkeypatch.setattr(
        compiler, "_distill_legacy", lambda *a, **k: calls.append("legacy") or {}
    )
    monkeypatch.setattr(
        compiler, "_distill_v2", lambda *a, **k: calls.append("v2") or {}
    )
    compiler.distill("n", [], {"content": _legacy_artifact()}, _LOG, "2025-06-30")
    compiler.distill("n", [], {"content": _two_island_artifact()}, _LOG, "2025-06-30")
    assert calls == ["legacy", "v2"]
