"""WS11-C: the structure extractor behind a protocol, and server-side validation.

The deterministic engine must propose nothing it cannot prove. The LLM engine may
propose anything, and `validate_plan` must drop whatever it cannot verify --
without ever raising.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from server import artifact, call_log, extractor, fingerprint
from server.db import ANCHOR_DATE, get_meta_connection

_RACE_JS = "[{qtr: 'Q1', hca: 10, uhs: 30}, {qtr: 'Q2', hca: 20, uhs: 35}]"
_RACE_COLUMNS = ["qtr", "hca", "uhs"]
_RACE_ROWS = [{"qtr": "Q1", "hca": 10, "uhs": 30}, {"qtr": "Q2", "hca": 20, "uhs": 35}]


def _call(result_name, columns, rows, sql="SELECT 1"):
    fp, canonical = call_log.fingerprint_result(columns, rows)
    return {
        "result_name": result_name,
        "sql_text": sql,
        "tool_name": "execute_sql",
        "result_fingerprint": fp,
        "result_rows": call_log.canonical_result(columns, rows) if canonical else None,
    }


@pytest.fixture
def session():
    return extractor.SessionContext(
        conversation_id="extractor-test",
        calls=[_call("race", _RACE_COLUMNS, _RACE_ROWS)],
        anchor_date=ANCHOR_DATE,
    )


_HTML = (
    f"<div><script>const RACE = {_RACE_JS};</script>"
    "<p>The gap has narrowed steadily across every quarter of the window shown.</p>"
    "</div>"
)


@pytest.fixture
def report(session):
    return fingerprint.match(_HTML, session.calls)


# A page with one real number on it, so value bindings have something to name.
_HTML_WITH_NUMBER = _HTML.replace("</div>", "<div><strong>10</strong></div></div>")


@pytest.fixture
def scalar_report(session):
    report = fingerprint.match(_HTML_WITH_NUMBER, session.calls)
    assert [s.value_id for s in report.scalars] == ["value:0"]
    return report


# ---------------------------------------------------------------------------
# Filter inference
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("1,234", [["thousands", []]]),
        ("+1,234", [["signed", []], ["thousands", []]]),
        ("34.5%", [["pct", [1]]]),
        ("34.56%", [["pct", [2]]]),
        ("-2.7pp", [["pp", []]]),
        ("662", []),
    ],
)
def test_filters_are_read_back_off_the_displayed_number(text, expected):
    assert extractor.infer_filters(text) == expected


def test_inferred_filters_are_all_in_the_compiler_whitelist():
    for text in ("+1,234", "34.5%", "-2.7pp", "1,000"):
        for name, args in extractor.infer_filters(text):
            assert name in artifact.FILTER_WHITELIST
            assert artifact.FILTER_WHITELIST[name] == len(args)


# ---------------------------------------------------------------------------
# The deterministic engine
# ---------------------------------------------------------------------------


def test_deterministic_plan_passes_fingerprint_matches_through(report, session):
    plan = extractor.DeterministicExtractor().propose(_HTML, report, session)
    assert plan["islands"] == [
        {"blob_id": "const:RACE", "result_name": "race", "origin": "fingerprint"}
    ]


def test_deterministic_plan_contains_no_inferences(report, session):
    plan = extractor.DeterministicExtractor().propose(_HTML, report, session)
    assert plan["derived_queries"] == []
    assert plan["charts"] == []
    assert not extractor.has_inferences(plan)


def test_deterministic_plan_classifies_all_prose_as_editorial(report, session):
    """Freeze-and-date is the safe default; regenerating text must be opted into."""
    plan = extractor.DeterministicExtractor().propose(_HTML, report, session)
    assert [b["tier"] for b in plan["narrative"]] == ["editorial"]
    assert plan["narrative"][0]["authored_as_of"] == ANCHOR_DATE


def test_get_extractor_defaults_to_deterministic(monkeypatch):
    monkeypatch.delenv("POC_DISTILLER", raising=False)
    assert isinstance(extractor.get_extractor(), extractor.DeterministicExtractor)


@pytest.mark.parametrize("value", ["anthropic", "llm", "claude", "ANTHROPIC"])
def test_get_extractor_opts_in_on_the_env_var(monkeypatch, value):
    monkeypatch.setenv("POC_DISTILLER", value)
    assert isinstance(extractor.get_extractor(), extractor.AnthropicExtractor)


# ---------------------------------------------------------------------------
# validate_plan -- the server-side gate
# ---------------------------------------------------------------------------


def test_validate_never_raises_on_garbage(report, session):
    for garbage in (None, 42, "plan", [], {"islands": "nope", "narrative": [1, 2]}):
        plan, warnings = extractor.validate_plan(garbage, report, session)
        assert isinstance(plan, dict) and isinstance(warnings, list)


def test_an_island_mapping_contradicting_a_fingerprint_is_dropped(report, session):
    """A fingerprint match is fact, not opinion. The model may not overrule it."""
    plan = {**extractor.empty_plan(), "islands": [
        {"blob_id": "const:RACE", "result_name": "some_other_result", "origin": "extractor"}
    ]}
    clean, warnings = extractor.validate_plan(plan, report, session)
    assert clean["islands"] == []
    assert any("contradicts the fingerprint match" in w for w in warnings)


def test_an_island_mapping_for_an_unknown_blob_is_dropped(report, session):
    plan = {**extractor.empty_plan(), "islands": [{"blob_id": "const:GHOST", "result_name": "race"}]}
    clean, warnings = extractor.validate_plan(plan, report, session)
    assert clean["islands"] == []
    assert any("no such blob" in w for w in warnings)


def test_a_fingerprint_island_mapping_survives(report, session):
    plan = {**extractor.empty_plan(), "islands": [
        {"blob_id": "const:RACE", "result_name": "race", "origin": "fingerprint"}
    ]}
    clean, warnings = extractor.validate_plan(plan, report, session)
    assert clean["islands"] and not warnings


def test_a_chart_with_an_unknown_type_is_dropped(report, session):
    plan = {**extractor.empty_plan(), "charts": [{"type": "pie", "result": "race", "id": "c1"}]}
    clean, warnings = extractor.validate_plan(plan, report, session)
    assert clean["charts"] == []
    assert any("unknown type" in w for w in warnings)


def test_a_chart_referencing_a_missing_field_is_dropped(report, session):
    plan = {**extractor.empty_plan(), "charts": [
        {"type": "line", "result": "race", "id": "c1", "x": "qtr",
         "series": [{"field": "nonexistent"}]}
    ]}
    clean, warnings = extractor.validate_plan(plan, report, session)
    assert clean["charts"] == []
    assert any("no field 'nonexistent'" in w for w in warnings)


def test_a_valid_chart_survives(report, session):
    spec = {"type": "line", "result": "race", "id": "c1", "x": "qtr",
            "series": [{"field": "hca"}, {"field": "uhs"}]}
    clean, warnings = extractor.validate_plan(
        {**extractor.empty_plan(), "charts": [spec]}, report, session
    )
    assert clean["charts"] == [spec]


def test_a_value_naming_a_number_that_is_not_on_the_page_is_dropped(scalar_report, session):
    """The model may not invent value_ids; it can only bind numbers we found."""
    plan = {**extractor.empty_plan(), "values": [
        {"value_id": "v_invented", "result": "race", "field": "hca", "filters": []}
    ]}
    clean, warnings = extractor.validate_plan(plan, scalar_report, session)
    assert clean["values"] == []
    assert any("no such number on the page" in w for w in warnings)


def test_a_value_with_an_unknown_filter_is_dropped(scalar_report, session):
    plan = {**extractor.empty_plan(), "values": [
        {"value_id": "value:0", "result": "race", "field": "hca", "filters": [["shout", []]]}
    ]}
    clean, warnings = extractor.validate_plan(plan, scalar_report, session)
    assert clean["values"] == []
    assert any("unknown display filter" in w for w in warnings)


def test_a_value_naming_a_missing_field_is_dropped(scalar_report, session):
    plan = {**extractor.empty_plan(), "values": [
        {"value_id": "value:0", "result": "race", "field": "ghost", "filters": []}
    ]}
    clean, warnings = extractor.validate_plan(plan, scalar_report, session)
    assert clean["values"] == []
    assert any("no field 'ghost'" in w for w in warnings)


def test_a_valid_value_binding_survives(scalar_report, session):
    entry = {"value_id": "value:0", "result": "race", "field": "hca",
             "filters": [["thousands", []]], "selector": ["index", 0]}
    clean, warnings = extractor.validate_plan(
        {**extractor.empty_plan(), "values": [entry]}, scalar_report, session
    )
    assert clean["values"] == [entry] and not warnings


def test_a_value_binding_defaults_to_the_first_row(scalar_report, session):
    entry = {"value_id": "value:0", "result": "race", "field": "hca", "filters": []}
    clean, _ = extractor.validate_plan(
        {**extractor.empty_plan(), "values": [entry]}, scalar_report, session
    )
    assert clean["values"][0]["selector"] == ["index", 0]


@pytest.mark.parametrize(
    "selector",
    [
        "[.kpi-card:nth-of-type(3)='strong']",  # a CSS selector, as a model once produced
        ["css", ".kpi"],
        ["index", "first"],
        ["match", "qtr"],
    ],
)
def test_an_unusable_selector_is_dropped(scalar_report, session, selector):
    """A malformed selector would become a data-value the parser rejects."""
    entry = {"value_id": "value:0", "result": "race", "field": "hca",
             "filters": [], "selector": selector}
    clean, warnings = extractor.validate_plan(
        {**extractor.empty_plan(), "values": [entry]}, scalar_report, session
    )
    assert clean["values"] == [] and warnings


def test_an_empty_selector_is_treated_as_missing_and_then_verified(scalar_report, session):
    """Defaulting to row 0 is safe only because the row is checked against the page."""
    entry = {"value_id": "value:0", "result": "race", "field": "hca",
             "filters": [], "selector": []}
    clean, _ = extractor.validate_plan(
        {**extractor.empty_plan(), "values": [entry]}, scalar_report, session
    )
    assert clean["values"][0]["selector"] == ["index", 0]  # row 0 does hold 10

    wrong = {**entry, "field": "uhs"}  # row 0 holds 30, the page shows 10
    clean, warnings = extractor.validate_plan(
        {**extractor.empty_plan(), "values": [wrong]}, scalar_report, session
    )
    assert clean["values"] == [] and any("but the page shows" in w for w in warnings)


def test_a_selector_pointing_at_the_wrong_row_is_dropped(scalar_report, session):
    """The page shows 10 (row 0). Row 1 holds 20, so this binding is a lie.

    Parity would never catch it: it compares this artifact against this data, and
    the number rendered today would be right. It is the next replay that breaks.
    """
    entry = {"value_id": "value:0", "result": "race", "field": "hca",
             "filters": [], "selector": ["index", 1]}
    clean, warnings = extractor.validate_plan(
        {**extractor.empty_plan(), "values": [entry]}, scalar_report, session
    )
    assert clean["values"] == []
    assert any("but the page shows" in w for w in warnings)


def test_a_selector_row_out_of_range_is_dropped(scalar_report, session):
    entry = {"value_id": "value:0", "result": "race", "field": "hca",
             "filters": [], "selector": ["index", 99]}
    clean, warnings = extractor.validate_plan(
        {**extractor.empty_plan(), "values": [entry]}, scalar_report, session
    )
    assert clean["values"] == []
    assert any("out of range" in w for w in warnings)


def test_a_match_selector_that_finds_the_right_row_survives(scalar_report, session):
    entry = {"value_id": "value:0", "result": "race", "field": "hca",
             "filters": [], "selector": ["match", "qtr", "Q1"]}
    clean, warnings = extractor.validate_plan(
        {**extractor.empty_plan(), "values": [entry]}, scalar_report, session
    )
    assert clean["values"] == [entry] and not warnings


def test_a_match_selector_naming_a_missing_row_is_dropped(scalar_report, session):
    entry = {"value_id": "value:0", "result": "race", "field": "hca",
             "filters": [], "selector": ["match", "qtr", "Q9"]}
    clean, warnings = extractor.validate_plan(
        {**extractor.empty_plan(), "values": [entry]}, scalar_report, session
    )
    assert clean["values"] == []
    assert any("no row where qtr = 'Q9'" in w for w in warnings)


def test_the_deterministic_extractors_own_selectors_validate(session):
    """Whatever the matcher produces must survive the gate it feeds."""
    html = _HTML_WITH_NUMBER
    report = fingerprint.match(html, session.calls)
    plan = extractor.DeterministicExtractor().propose(html, report, session)
    clean, warnings = extractor.validate_plan(plan, report, session)
    assert clean["values"] == plan["values"]
    assert not warnings


def test_an_analytical_tier_without_a_goal_falls_back_to_editorial(report, session):
    plan = {**extractor.empty_plan(), "narrative": [{"block_id": "b0", "tier": "analytical"}]}
    clean, warnings = extractor.validate_plan(plan, report, session)
    assert clean["narrative"][0]["tier"] == "editorial"
    assert any("no goal given" in w for w in warnings)


def test_an_unparseable_watch_is_dropped_but_the_block_survives(report, session):
    plan = {**extractor.empty_plan(), "narrative": [
        {"block_id": "b0", "tier": "editorial", "watch": "this is not a watch"}
    ]}
    clean, warnings = extractor.validate_plan(plan, report, session)
    assert "watch" not in clean["narrative"][0]
    assert any("watch on b0" in w for w in warnings)


def test_a_valid_watch_survives(report, session):
    plan = {**extractor.empty_plan(), "narrative": [
        {"block_id": "b0", "tier": "editorial", "watch": "race[last].uhs < 800"}
    ]}
    clean, warnings = extractor.validate_plan(plan, report, session)
    assert clean["narrative"][0]["watch"] == "race[last].uhs < 800"
    assert not warnings


def test_a_proposed_watch_on_a_non_editorial_block_is_dropped(report, session):
    """A model proposal is dropped with a warning; only a human override errors."""
    plan = {**extractor.empty_plan(), "narrative": [
        {"block_id": "b0", "tier": "analytical", "goal": "explain",
         "watch": "race[last].uhs < 800"}
    ]}
    clean, warnings = extractor.validate_plan(plan, report, session)
    assert "watch" not in clean["narrative"][0]
    assert clean["narrative"][0]["tier"] == "analytical"  # the tier itself survives
    assert any("not 'editorial'" in w for w in warnings)


def test_a_watch_survives_the_tier_falling_back_to_editorial(report, session):
    """analytical-without-a-goal falls back to editorial, which a watch is allowed on."""
    plan = {**extractor.empty_plan(), "narrative": [
        {"block_id": "b0", "tier": "analytical", "watch": "race[last].uhs < 800"}
    ]}
    clean, warnings = extractor.validate_plan(plan, report, session)
    assert clean["narrative"][0]["tier"] == "editorial"
    assert clean["narrative"][0]["watch"] == "race[last].uhs < 800"


def test_the_model_can_propose_a_watch(monkeypatch, report, session):
    reply = json.dumps({"narrative": [
        {"block_id": "b0", "tier": "editorial", "watch": "race[0].hca < 500"}
    ]})
    module, _ = _fake_anthropic([reply])
    monkeypatch.setitem(sys.modules, "anthropic", module)
    plan = extractor.AnthropicExtractor().propose(_HTML, report, session)
    clean, warnings = extractor.validate_plan(plan, report, session)
    assert clean["narrative"][0]["watch"] == "race[0].hca < 500"


def test_a_model_proposed_watch_with_bad_grammar_is_dropped(monkeypatch, report, session):
    reply = json.dumps({"narrative": [
        {"block_id": "b0", "tier": "editorial", "watch": "race[0].hca <"}
    ]})
    module, _ = _fake_anthropic([reply])
    monkeypatch.setitem(sys.modules, "anthropic", module)
    plan = extractor.AnthropicExtractor().propose(_HTML, report, session)
    clean, warnings = extractor.validate_plan(plan, report, session)
    assert "watch" not in clean["narrative"][0]
    assert any("watch on b0" in w for w in warnings)


def test_the_system_prompt_offers_watch_conservatively():
    """The model must not invent a threshold the author never wrote."""
    assert '"watch": str?' in extractor._SYSTEM
    assert "Never invent a threshold" in extractor._SYSTEM


def test_malformed_tabs_are_dropped(report, session):
    plan = {**extractor.empty_plan(), "tabs": [{"id": "a"}]}
    clean, warnings = extractor.validate_plan(plan, report, session)
    assert clean["tabs"] is None
    assert any("id and a label" in w for w in warnings)


# ---------------------------------------------------------------------------
# Derived queries: proven against real data, or dropped
# ---------------------------------------------------------------------------


@pytest.fixture
def db_session():
    """A session backed by the real warehouse, so derived SQL can actually run."""
    from server import tools

    cid = "extractor-derived"
    tools.execute_sql(cid, "SELECT division FROM facilities ORDER BY division LIMIT 3", "divs")
    return extractor.SessionContext(
        conversation_id=cid,
        calls=call_log.fetch(get_meta_connection(), cid),
        anchor_date=ANCHOR_DATE,
    )


def _blob_html(rows):
    body = ", ".join("{bed_count: %d}" % r for r in rows)
    return f"<div><script>const BEDS = [{body}];</script></div>"


def test_a_derived_query_that_is_not_a_select_is_dropped(db_session):
    report = fingerprint.match("<div></div>", db_session.calls)
    plan = {**extractor.empty_plan(), "derived_queries": [
        {"result_name": "bad", "sql": "DELETE FROM facilities", "covers": []}
    ]}
    clean, warnings = extractor.validate_plan(plan, report, db_session)
    assert clean["derived_queries"] == []
    assert any("Only SELECT statements are allowed" in w for w in warnings)


def test_a_derived_query_that_fails_dry_run_is_dropped(db_session):
    report = fingerprint.match("<div></div>", db_session.calls)
    plan = {**extractor.empty_plan(), "derived_queries": [
        {"result_name": "bad", "sql": "SELECT * FROM no_such_table", "covers": []}
    ]}
    clean, warnings = extractor.validate_plan(plan, report, db_session)
    assert clean["derived_queries"] == []
    assert any("dry run failed" in w for w in warnings)


def test_a_derived_query_that_does_not_reproduce_its_target_is_dropped(db_session):
    """It ran fine, but its rows are not the blob's rows. Coverage is proven, not claimed."""
    html = _blob_html([1, 2, 3])
    report = fingerprint.match(html, db_session.calls)
    plan = {**extractor.empty_plan(), "derived_queries": [
        {"result_name": "beds", "covers": ["const:BEDS"],
         "sql": "SELECT bed_count FROM facilities ORDER BY bed_count LIMIT 3"}
    ]}
    clean, warnings = extractor.validate_plan(plan, report, db_session)
    assert clean["derived_queries"] == []
    assert any("do not reproduce" in w for w in warnings)


def test_a_derived_query_that_reproduces_its_target_survives_and_is_logged(db_session):
    from server import tools

    real = tools.execute_sql(
        db_session.conversation_id,
        "SELECT bed_count FROM facilities ORDER BY bed_count LIMIT 3",
        "probe",
    )
    beds = [row["bed_count"] for row in real["rows"]]
    html = _blob_html(beds)
    calls = call_log.fetch(get_meta_connection(), db_session.conversation_id)
    session = extractor.SessionContext(db_session.conversation_id, calls, ANCHOR_DATE)
    report = fingerprint.match(html, calls)

    plan = {**extractor.empty_plan(), "derived_queries": [
        {"result_name": "beds_derived", "covers": ["const:BEDS"],
         "sql": "SELECT bed_count FROM facilities ORDER BY bed_count LIMIT 3"}
    ]}
    clean, warnings = extractor.validate_plan(plan, report, session)

    assert [d["result_name"] for d in clean["derived_queries"]] == ["beds_derived"]
    assert clean["derived_queries"][0]["origin"] == "extractor"
    # Lineage stays complete: the verified query is in the log as save_derive.
    logged = call_log.fetch(get_meta_connection(), db_session.conversation_id)
    derived = [c for c in logged if c["tool_name"] == "save_derive"]
    assert derived and derived[-1]["result_name"] == "beds_derived"


def test_a_derived_query_can_cover_a_single_number(db_session):
    """`covers` may name a value_id; the number must appear in the derived rows."""
    from server import tools

    real = tools.execute_sql(
        db_session.conversation_id, "SELECT MAX(bed_count) AS m FROM facilities", "probe_max"
    )
    biggest = real["rows"][0]["m"]
    html = f"<div><p>The largest facility has <strong>{biggest}</strong> beds.</p></div>"
    calls = call_log.fetch(get_meta_connection(), db_session.conversation_id)
    session = extractor.SessionContext(db_session.conversation_id, calls, ANCHOR_DATE)
    report = fingerprint.match(html, calls)
    value_id = report.scalars[0].value_id

    plan = {**extractor.empty_plan(), "derived_queries": [
        {"result_name": "max_beds", "covers": [value_id],
         "sql": "SELECT MAX(bed_count) AS m FROM facilities"}
    ]}
    clean, warnings = extractor.validate_plan(plan, report, session)
    assert [d["result_name"] for d in clean["derived_queries"]] == ["max_beds"]


def test_a_derived_query_covering_a_number_it_does_not_produce_is_dropped(db_session):
    html = "<div><p>The answer is <strong>999999</strong> beds in total.</p></div>"
    calls = call_log.fetch(get_meta_connection(), db_session.conversation_id)
    session = extractor.SessionContext(db_session.conversation_id, calls, ANCHOR_DATE)
    report = fingerprint.match(html, calls)
    value_id = report.scalars[0].value_id

    plan = {**extractor.empty_plan(), "derived_queries": [
        {"result_name": "min_beds", "covers": [value_id],
         "sql": "SELECT MIN(bed_count) AS m FROM facilities"}
    ]}
    clean, warnings = extractor.validate_plan(plan, report, session)
    assert clean["derived_queries"] == []
    assert any("do not contain" in w for w in warnings)


def test_a_derived_query_makes_the_plan_an_inference(db_session):
    plan = {**extractor.empty_plan(), "derived_queries": [{"result_name": "x", "sql": "SELECT 1"}]}
    assert extractor.has_inferences(plan)


def test_a_non_editorial_tier_makes_the_plan_an_inference():
    plan = {**extractor.empty_plan(), "narrative": [{"block_id": "b0", "tier": "analytical"}]}
    assert extractor.has_inferences(plan)


def test_an_extractor_added_island_makes_the_plan_an_inference():
    plan = {**extractor.empty_plan(), "islands": [
        {"blob_id": "b", "result_name": "r", "origin": "extractor"}
    ]}
    assert extractor.has_inferences(plan)


# ---------------------------------------------------------------------------
# AnthropicExtractor: never crashes, always degrades
# ---------------------------------------------------------------------------


def _fake_anthropic(replies):
    """A stand-in SDK whose client returns the given texts in order."""
    calls = {"n": 0}

    class _Block:
        type = "text"

        def __init__(self, text):
            self.text = text

    class _Response:
        def __init__(self, text):
            self.content = [_Block(text)]

    class _Messages:
        def create(self, **kwargs):
            reply = replies[min(calls["n"], len(replies) - 1)]
            calls["n"] += 1
            if isinstance(reply, Exception):
                raise reply
            return _Response(reply)

    class _Client:
        def __init__(self, *a, **k):
            self.messages = _Messages()

    module = types.ModuleType("anthropic")
    module.Anthropic = _Client
    return module, calls


def test_missing_sdk_falls_back_to_the_deterministic_plan(monkeypatch, report, session):
    monkeypatch.setitem(sys.modules, "anthropic", None)
    plan = extractor.AnthropicExtractor().propose(_HTML, report, session)
    assert plan["islands"][0]["origin"] == "fingerprint"
    assert not extractor.has_inferences(plan)


def test_invalid_json_retries_once_then_falls_back(monkeypatch, report, session):
    module, calls = _fake_anthropic(["not json", "still not json"])
    monkeypatch.setitem(sys.modules, "anthropic", module)
    plan = extractor.AnthropicExtractor().propose(_HTML, report, session)
    assert calls["n"] == 2  # one retry, then give up
    assert not extractor.has_inferences(plan)


def test_valid_json_on_the_retry_is_used(monkeypatch, report, session):
    good = json.dumps({"tabs": [{"id": "a", "label": "A"}]})
    module, calls = _fake_anthropic(["oops", good])
    monkeypatch.setitem(sys.modules, "anthropic", module)
    plan = extractor.AnthropicExtractor().propose(_HTML, report, session)
    assert calls["n"] == 2
    assert plan["tabs"] == [{"id": "a", "label": "A"}]


def test_a_network_error_falls_back(monkeypatch, report, session):
    module, _ = _fake_anthropic([RuntimeError("connection reset")])
    monkeypatch.setitem(sys.modules, "anthropic", module)
    plan = extractor.AnthropicExtractor().propose(_HTML, report, session)
    assert not extractor.has_inferences(plan)


def test_the_model_may_not_overrule_a_fingerprint_match(monkeypatch, report, session):
    """Even before validate_plan, the merge refuses to replace a matched island."""
    reply = json.dumps({"islands": [{"blob_id": "const:RACE", "result_name": "wrong"}]})
    module, _ = _fake_anthropic([reply])
    monkeypatch.setitem(sys.modules, "anthropic", module)
    plan = extractor.AnthropicExtractor().propose(_HTML, report, session)
    assert plan["islands"] == [
        {"blob_id": "const:RACE", "result_name": "race", "origin": "fingerprint"}
    ]


def test_a_code_fenced_reply_is_parsed(monkeypatch, report, session):
    reply = "```json\n" + json.dumps({"tabs": [{"id": "a", "label": "A"}]}) + "\n```"
    module, _ = _fake_anthropic([reply])
    monkeypatch.setitem(sys.modules, "anthropic", module)
    plan = extractor.AnthropicExtractor().propose(_HTML, report, session)
    assert plan["tabs"] == [{"id": "a", "label": "A"}]


def test_the_model_can_reclassify_a_prose_block(monkeypatch, report, session):
    reply = json.dumps({"narrative": [{"block_id": "b0", "tier": "analytical", "goal": "explain"}]})
    module, _ = _fake_anthropic([reply])
    monkeypatch.setitem(sys.modules, "anthropic", module)
    plan = extractor.AnthropicExtractor().propose(_HTML, report, session)
    assert plan["narrative"][0]["tier"] == "analytical"
    assert extractor.has_inferences(plan)  # so it must be confirmed
