"""WS11-E: the structure-confirmation round-trip in save_report_definition.

Fingerprint matches are fact and register in one call. Inferences -- a derived
query, a reclassified narrative block, an engine-added island -- must be shown to
the client and confirmed before anything is registered.
"""

from __future__ import annotations

import json

import pytest

from server import artifact, call_log, extraction_cache, registry, tools
from server.db import ANCHOR_DATE, get_meta_connection

_ADMISSIONS = (
    "SELECT f.division, COUNT(*) AS admissions "
    "FROM admissions a JOIN facilities f ON a.facility_id = f.facility_id "
    "WHERE a.admit_date >= DATE '2025-06-01' AND a.admit_date < DATE '2025-07-01' "
    "GROUP BY f.division ORDER BY f.division"
)


def _js_rows(rows, columns):
    body = ", ".join(
        "{" + ", ".join(f"{c}: {json.dumps(r[c])}" for c in columns) + "}" for r in rows
    )
    return f"[{body}]"


def _free_form_page(rows, columns, prose="HCA should press its advantage in surgical lines."):
    return (
        "<div>"
        f"<script>const ADM = {_js_rows(rows, columns)};\n"
        "function draw() { ADM.forEach(function (d) { console.log(d.division); }); }</script>"
        f"<p>{prose} It is the clearest opportunity in the current window.</p>"
        "</div>"
    )


@pytest.fixture
def session(request):
    """A conversation with one logged query, keyed uniquely per test."""
    cid = f"confirm-{request.node.name}"
    result = tools.execute_sql(cid, _ADMISSIONS, "admissions_by_division")
    assert "error" not in result
    return cid, result


def _save(cid, artifact_html, name, confirmations=None):
    return tools.save_report_definition(
        cid,
        name,
        [],
        {"format": "html", "title": name, "content": artifact_html},
        None,
        confirmations,
    )


# ---------------------------------------------------------------------------
# The one-call path: everything fingerprinted, nothing inferred
# ---------------------------------------------------------------------------


def test_a_fully_fingerprinted_page_registers_in_one_call(session):
    cid, result = session
    html = _free_form_page(result["rows"], result["columns"])
    assert artifact.detect_mode(html) == "free_form"

    saved = _save(cid, html, "FF One Call")
    assert saved["status"] == "registered", saved
    assert saved["parity"]["passed"]


def test_the_registered_definition_carries_the_island_and_the_query(session):
    cid, result = session
    html = _free_form_page(result["rows"], result["columns"])
    saved = _save(cid, html, "FF Definition Shape")
    assert saved["status"] == "registered", saved

    definition = registry.get(get_meta_connection(), saved["report_id"])
    names = [q["result_name"] for q in definition["parameterized_sql"]]
    assert names == ["admissions_by_division"]
    # The date literals were tokenized on the way in.
    assert "__REPORT_DATE__" in definition["parameterized_sql"][0]["sql"]
    assert "function __ISLAND__(" in definition["rendering_spec"]["template"]


def test_the_free_form_prose_is_frozen_as_an_editorial_block(session):
    cid, result = session
    html = _free_form_page(result["rows"], result["columns"])
    saved = _save(cid, html, "FF Editorial")
    definition = registry.get(get_meta_connection(), saved["report_id"])
    block = definition["editorial_blocks"][0]
    assert block["authored_as_of"] == ANCHOR_DATE
    assert block["html_sha256"]


# ---------------------------------------------------------------------------
# Contract artifacts never enter the normalizer
# ---------------------------------------------------------------------------


def test_a_v2_artifact_bypasses_extraction_entirely(session):
    from runner import render

    cid, result = session
    html = render.build_island("admissions_by_division", result) + (
        '<span data-value="admissions_by_division[0].admissions">'
        f"{result['rows'][0]['admissions']}</span>"
    )
    assert artifact.detect_mode(html) == "v2"
    saved = _save(cid, html, "V2 Bypass")
    assert saved["status"] == "registered", saved
    definition = registry.get(get_meta_connection(), saved["report_id"])
    assert "__ISLAND__" not in definition["rendering_spec"]["template"]


def test_a_legacy_artifact_bypasses_extraction_entirely(session):
    from runner import render

    cid, result = session
    html = render.build_table_html(
        "admissions_by_division", result["columns"], result["rows"]
    )
    assert artifact.detect_mode(html) == "legacy"
    saved = _save(cid, html, "Legacy Bypass")
    assert saved["status"] == "registered", saved
    assert "__ISLAND__" not in registry.get(
        get_meta_connection(), saved["report_id"]
    )["rendering_spec"]["template"]


# ---------------------------------------------------------------------------
# The round-trip: an inference must be confirmed
# ---------------------------------------------------------------------------


def _plan_with_derived(cid, rows, columns):
    """Cache a plan whose derived query really does reproduce the page's blob."""
    from server import extractor, fingerprint

    html = _free_form_page(rows, columns)
    calls = call_log.fetch(get_meta_connection(), cid)
    report = fingerprint.match(html, calls)
    ctx = extractor.SessionContext(cid, calls, ANCHOR_DATE)
    plan = extractor.DeterministicExtractor().propose(html, report, ctx)
    plan["derived_queries"] = [
        {"result_name": "adm_derived", "sql": _ADMISSIONS, "covers": [], "origin": "extractor"}
    ]
    return html, plan


def test_an_inference_returns_a_confirmation_token_and_registers_nothing(session, monkeypatch):
    from server import extractor

    cid, result = session
    html, plan = _plan_with_derived(cid, result["rows"], result["columns"])

    class _Proposer:
        def propose(self, html_, report_, session_):
            return plan

    monkeypatch.setattr(extractor, "get_extractor", lambda: _Proposer())

    saved = _save(cid, html, "FF Needs Confirmation")
    assert saved["status"] == "needs_structure_confirmation"
    assert saved["report_id"] is None
    assert len(saved["confirmation_token"]) == 64
    derived = saved["extraction"]["derived_queries"]
    assert derived[0]["result_name"] == "adm_derived"
    assert "SELECT" in derived[0]["sql"]  # the SQL is visible before you accept it
    assert saved["extraction"]["matched_islands"][0]["result_name"] == "admissions_by_division"


def test_accept_all_on_the_second_call_registers(session, monkeypatch):
    from server import extractor

    cid, result = session
    html, plan = _plan_with_derived(cid, result["rows"], result["columns"])
    monkeypatch.setattr(extractor, "get_extractor", lambda: type("P", (), {"propose": lambda *_: plan})())

    first = _save(cid, html, "FF Accept All")
    token = first["confirmation_token"]

    second = _save(cid, html, "FF Accept All", [{"token": token, "accept_all": True}])
    assert second["status"] == "registered", second
    assert second["parity"]["passed"]

    # The derived query covered nothing the artifact references, so it does not
    # enter the definition -- but its execution is still recorded as lineage.
    definition = registry.get(get_meta_connection(), second["report_id"])
    assert "adm_derived" not in [q["result_name"] for q in definition["parameterized_sql"]]
    logged = call_log.fetch(get_meta_connection(), cid)
    derived = [c for c in logged if c["tool_name"] == "save_derive"]
    assert derived and derived[-1]["result_name"] == "adm_derived"


def test_rejecting_a_derived_query_drops_it(session, monkeypatch):
    from server import extractor

    cid, result = session
    html, plan = _plan_with_derived(cid, result["rows"], result["columns"])
    monkeypatch.setattr(extractor, "get_extractor", lambda: type("P", (), {"propose": lambda *_: plan})())

    first = _save(cid, html, "FF Reject")
    token = first["confirmation_token"]
    second = _save(
        cid, html, "FF Reject", [{"token": token}, {"derived": "adm_derived", "accept": False}]
    )
    assert second["status"] == "registered", second
    definition = registry.get(get_meta_connection(), second["report_id"])
    assert "adm_derived" not in [q["result_name"] for q in definition["parameterized_sql"]]


def test_an_override_can_flip_a_block_back_to_editorial(session, monkeypatch):
    from server import extractor, fingerprint

    cid, result = session
    html = _free_form_page(result["rows"], result["columns"])
    calls = call_log.fetch(get_meta_connection(), cid)
    report = fingerprint.match(html, calls)
    ctx = extractor.SessionContext(cid, calls, ANCHOR_DATE)
    plan = extractor.DeterministicExtractor().propose(html, report, ctx)
    plan["narrative"][0].update({"tier": "analytical", "goal": "summarize", "inputs": ["admissions_by_division"]})
    monkeypatch.setattr(extractor, "get_extractor", lambda: type("P", (), {"propose": lambda *_: plan})())

    first = _save(cid, html, "FF Flip Tier")
    assert first["status"] == "needs_structure_confirmation"
    token = first["confirmation_token"]
    assert first["extraction"]["narrative"][0]["tier"] == "analytical"

    second = _save(
        cid, html, "FF Flip Tier",
        [{"token": token, "accept_all": True}, {"block_id": "b0", "tier": "editorial"}],
    )
    assert second["status"] == "registered", second
    definition = registry.get(get_meta_connection(), second["report_id"])
    assert definition["editorial_blocks"], "the block should be frozen, not regenerated"
    assert definition["reasoning_steps"] == []


# ---------------------------------------------------------------------------
# Token handling
# ---------------------------------------------------------------------------


def test_a_confirmation_without_a_token_is_refused(session, monkeypatch):
    from server import extractor

    cid, result = session
    html, plan = _plan_with_derived(cid, result["rows"], result["columns"])
    monkeypatch.setattr(extractor, "get_extractor", lambda: type("P", (), {"propose": lambda *_: plan})())

    saved = _save(cid, html, "FF No Token", [{"accept_all": True}])
    assert saved["status"] == "invalid_structure_confirmation"
    assert "confirmation_token" in saved["error"]


def test_an_unknown_token_is_refused_and_registers_nothing(session):
    cid, result = session
    html = _free_form_page(result["rows"], result["columns"])
    saved = _save(cid, html, "FF Bad Token", [{"token": "0" * 64, "accept_all": True}])
    assert saved["status"] == "structure_confirmation_expired"
    with pytest.raises(KeyError):
        registry.get(get_meta_connection(), "ff_bad_token")


def test_a_token_from_another_conversation_is_refused(session, monkeypatch):
    from server import extractor

    cid, result = session
    html, plan = _plan_with_derived(cid, result["rows"], result["columns"])
    monkeypatch.setattr(extractor, "get_extractor", lambda: type("P", (), {"propose": lambda *_: plan})())
    first = _save(cid, html, "FF Cross Convo")
    token = first["confirmation_token"]

    other = f"{cid}-other"
    tools.execute_sql(other, _ADMISSIONS, "admissions_by_division")
    saved = _save(other, html, "FF Cross Convo", [{"token": token, "accept_all": True}])
    assert saved["status"] == "structure_confirmation_expired"


def test_the_cached_plan_is_reused_not_reproposed(session, monkeypatch):
    """The second call must apply the plan the client saw, not ask the model again."""
    from server import extractor

    cid, result = session
    html, plan = _plan_with_derived(cid, result["rows"], result["columns"])
    proposals = {"n": 0}

    class _Counting:
        def propose(self, *_):
            proposals["n"] += 1
            return plan

    monkeypatch.setattr(extractor, "get_extractor", lambda: _Counting())
    first = _save(cid, html, "FF Cache")
    assert proposals["n"] == 1
    _save(cid, html, "FF Cache", [{"token": first["confirmation_token"], "accept_all": True}])
    assert proposals["n"] == 1, "the extractor was re-run on the confirmation call"


def test_the_plan_token_is_stable_across_serialization():
    plan = {"islands": [{"blob_id": "b", "result_name": "r"}], "values": []}
    assert extraction_cache.plan_token(plan) == extraction_cache.plan_token(json.loads(json.dumps(plan)))


def test_the_plan_token_changes_when_the_plan_changes():
    a = {"islands": [{"blob_id": "b", "result_name": "r"}]}
    b = {"islands": [{"blob_id": "b", "result_name": "other"}]}
    assert extraction_cache.plan_token(a) != extraction_cache.plan_token(b)


# ---------------------------------------------------------------------------
# Nothing is ever swallowed
# ---------------------------------------------------------------------------


def test_an_unmatched_constant_surfaces_in_the_warnings(session):
    cid, result = session
    html = (
        _free_form_page(result["rows"], result["columns"])
        + "<script>const MYSTERY = [{a: 1}, {a: 2}];</script>"
    )
    saved = _save(cid, html, "FF Unmatched")
    assert saved["status"] == "registered", saved
    assert any("MYSTERY" in w for w in saved["warnings"])


def test_a_javascript_computed_constant_is_named_in_the_warnings(session):
    cid, result = session
    html = (
        _free_form_page(result["rows"], result["columns"])
        + "<script>const RATIO = [{division: 'North', half: d.admissions / 2}];</script>"
    )
    saved = _save(cid, html, "FF Computed")
    assert saved["status"] == "registered", saved
    assert any("RATIO" in w and "computed in JavaScript" in w for w in saved["warnings"])
