"""Tests for the v2 renderer: filters, pick, islands, theme/layout."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest

from runner import render

_RACE = {
    "columns": ["qtr", "hca", "gap", "gap_trend"],
    "rows": [
        {"qtr": "Q1'24", "hca": 21666, "gap": 1393, "gap_trend": 0},
        {"qtr": "Q4'24", "hca": 22628, "gap": 865, "gap_trend": -155},
        {"qtr": "Q1'25", "hca": 22713, "gap": 662, "gap_trend": -203},
    ],
}


# --- filters -------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [(1234, "1,234"), (-1234, "-1,234"), (0, "0"), (1234.5, "1,234.5")],
)
def test_thousands(value, expected):
    assert render.do_thousands(value) == expected


@pytest.mark.parametrize(
    "value,expected", [(5, "+5"), (-5, "-5"), (0, "0"), (2.7, "+2.7")]
)
def test_signed(value, expected):
    assert render.do_signed(value) == expected


def test_pct_and_pp():
    assert render.do_pct(34.52) == "34.5%"
    assert render.do_pct(34.52, 2) == "34.52%"
    assert render.do_pp(-2.7) == "-2.7pp"
    assert render.do_pp(3) == "3pp"


def test_filters_compose_in_either_order():
    """`| signed | thousands` and `| thousands | signed` must agree."""
    assert render.do_thousands(render.do_signed(1234)) == "+1,234"
    assert render.do_signed(render.do_thousands(1234)) == "+1,234"
    assert render.do_pp(render.do_signed(2.7)) == "+2.7pp"
    assert render.do_signed(render.do_pct(2.34)) == "+2.3%"


def test_round_is_jinja_builtin_not_shadowed():
    """Shadowing `round` would change the legacy path; the builtin suffices."""
    assert "round" not in render._env.filters or render._env.filters[
        "round"
    ].__module__.startswith("jinja2")
    assert render._env.from_string("{{ 3.14159 | round(2) }}").render() == "3.14"


# --- pick ----------------------------------------------------------------


@pytest.mark.parametrize(
    "selector,expected",
    [
        (".", 1393),
        ("[0]", 1393),
        ("[first]", 1393),
        ("[last]", 662),
        ("[1]", 865),
        ("[qtr='Q4''24']", 865),
    ],
)
def test_pick_selectors(selector, expected):
    assert render.pick(_RACE, selector, "gap") == expected


@pytest.mark.parametrize(
    "selector,field",
    [
        ("[9]", "gap"),  # index out of range
        ("[qtr='nope']", "gap"),  # no matching row
        ("[bogus]", "gap"),  # unparseable selector
        ("[last]", "missing"),  # unknown field
    ],
)
def test_pick_raises_rather_than_rendering_blank(selector, field):
    with pytest.raises(render.SelectorError):
        render.pick(_RACE, selector, field)


def test_pick_raises_on_empty_result():
    with pytest.raises(render.SelectorError):
        render.pick({"columns": ["a"], "rows": []}, "[last]", "a")


def test_sign_class():
    assert render.sign_class(5) == "growth"
    assert render.sign_class(-5) == "decline"
    assert render.sign_class(0) == "flat"
    assert render.sign_class("not a number") == "flat"


# --- islands / tojson ----------------------------------------------------


def test_tojson_survives_duckdb_scalars_and_stays_script_safe():
    template = render._env.from_string("{{ rows | tojson }}")
    out = template.render(
        rows=[{"d": dt.date(2025, 1, 1), "v": Decimal("1.50"), "q": "Q1'25"}]
    )
    assert json.loads(out) == [{"d": "2025-01-01", "v": 1.5, "q": "Q1'25"}]
    # The apostrophe is \u-escaped, so an island cannot break out of its <script>.
    assert "'" not in out


def test_tojson_raises_on_a_type_that_cannot_round_trip():
    template = render._env.from_string("{{ rows | tojson }}")
    with pytest.raises(TypeError):
        template.render(rows=[{"x": object()}])


def test_build_island_matches_what_tojson_emits():
    """The client's island and the compiled template's island must agree byte for
    byte, or the parity gate compares two different serializations."""
    island = render.build_island("race", _RACE)
    payload = island.split(">", 1)[1].rsplit("<", 1)[0]
    assert json.loads(payload) == _RACE["rows"]
    assert "'" not in payload  # '-escaped, so it cannot close the <script>
    rendered = render._env.from_string("{{ rows | tojson }}").render(rows=_RACE["rows"])
    assert payload == rendered


# --- editorial banner ----------------------------------------------------


def test_editorial_banner_is_empty_when_nothing_is_stale():
    out = render._env.from_string("{{ editorial_banner('t') }}").render(stale_blocks={})
    assert out == ""


def test_editorial_banner_renders_unescaped_markup():
    """A plain str would render the banner's own tags as visible text."""
    out = render._env.from_string("{{ editorial_banner('t') }}").render(
        stale_blocks={"t": "Authored 2025-06-30; watch fired."}
    )
    assert out.startswith('<div class="staleness-banner"')
    assert "&lt;div" not in out
    assert "watch fired." in out


# --- layout --------------------------------------------------------------


def _tabbed_definition() -> dict:
    template = (
        render.build_island("race", _RACE)
        + '<div class="kpi-strip"><div class="kpi-card">'
        + "<span>{{ pick(race, '[last]', 'gap') | thousands }}</span></div></div>"
        + '<div data-section="one"><h2>One</h2>'
        + '<div id="c1" data-chart=\'{"type":"line","result":"race","x":"qtr",'
        '"series":[{"field":"gap","label":"Gap"}]}\'></div></div>'
        + '<div data-section="two"><h2>Two</h2></div>'
    )
    return {
        "report_name": "Race",
        "rendering_spec": {
            "title": "The Race",
            "template": template,
            "formats": ["html"],
            "layout": "tabbed-dashboard",
            "theme": "market-story-v1",
            "sections": [
                {"id": "one", "label": "First"},
                {"id": "two", "label": "Second"},
            ],
            "charts": [{"type": "line", "result": "race"}],
        },
    }


def test_tabbed_layout_inlines_runtime_theme_and_one_panel_per_section():
    html = render.render_html(_tabbed_definition(), {"race": _RACE}, as_of="2025-06-30")
    assert "charts_v1" in html and "function drawChart" in html
    assert ".staleness-banner" in html  # theme CSS inlined
    # Count in the page body only: the inlined runtime also mentions these roles.
    page = html.split("<script>", 1)[0]
    assert page.count('role="tabpanel"') == 2
    assert page.count('role="tab"') == 2
    assert 'id="panel-one"' in html and 'id="panel-two"' in html
    assert ">First<" in html and ">Second<" in html
    assert "2025-06-30" in html
    # The KPI strip has no data-section, so it stays above the tab bar.
    assert html.index('class="kpi-strip"') < html.index('class="tabs"')
    # The island rendered, and the value came through the filter.
    assert '"gap": 662' in html
    assert ">662<" in html


def test_tabbed_layout_theme_name_normalizes_dashes():
    definition = _tabbed_definition()
    definition["rendering_spec"]["theme"] = "market_story_v1"
    assert ".staleness-banner" in render.render_html(definition, {"race": _RACE})


def test_unknown_theme_fails_loudly():
    definition = _tabbed_definition()
    definition["rendering_spec"]["theme"] = "no-such-theme"
    with pytest.raises(FileNotFoundError):
        render.render_html(definition, {"race": _RACE})


# --- legacy path unchanged -----------------------------------------------

def test_legacy_render_uses_the_base_shell():
    """No layout key -> the default base template: shell + spliced body + runtime.

    The runtime is inlined so a non-tabbed v2 report's bound tables and value
    filters still work; it is a no-op for a v1 report like this one.
    """
    definition = {
        "report_name": "Legacy",
        "rendering_spec": {
            "title": "Legacy",
            "template": "<h1>Legacy</h1><p class=\"headline\">{{ occ.rows[0]['rate'] }}</p>",
            "formats": ["html"],
        },
    }
    results = {"occ": {"columns": ["rate"], "rows": [{"rate": 0.71}]}}
    out = render.render_html(definition, results)
    assert out.startswith("<!DOCTYPE html>")
    assert "<title>Legacy</title>" in out
    assert '<h1>Legacy</h1><p class="headline">0.71</p>' in out  # v1 body, verbatim
    assert "function fillTable" in out  # the runtime is inlined in the default layout
    assert 'data-tabs' not in out and 'role="tablist"' not in out  # no tabbed furniture
