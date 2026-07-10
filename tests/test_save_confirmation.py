"""WS11-E: the structure-confirmation round-trip in save_report_definition.

Fingerprint matches are fact and register in one call. Inferences -- a derived
query, a reclassified narrative block, an engine-added island -- must be shown to
the client and confirmed before anything is registered.
"""

from __future__ import annotations

import json

import pytest

from runner import regenerate
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


def test_a_consumed_token_cannot_register_a_second_report(session, monkeypatch):
    """A token is single-use. Replaying it must not quietly make another version."""
    from server import extractor

    cid, result = session
    html, plan = _plan_with_derived(cid, result["rows"], result["columns"])
    monkeypatch.setattr(extractor, "get_extractor", lambda: type("P", (), {"propose": lambda *_: plan})())

    first = _save(cid, html, "FF Single Use")
    token = first["confirmation_token"]
    second = _save(cid, html, "FF Single Use", [{"token": token, "accept_all": True}])
    assert second["status"] == "registered"

    third = _save(cid, html, "FF Single Use", [{"token": token, "accept_all": True}])
    assert third["status"] == "structure_confirmation_used"
    assert third["report_id"] == second["report_id"]
    assert "already used" in third["error"]

    # Still exactly one version: the replay registered nothing.
    versions = [
        r["definition_version"]
        for r in registry.list_all(get_meta_connection())
        if r["report_id"] == second["report_id"]
    ]
    assert versions == [second["definition_version"]]


def test_the_consumed_plan_is_kept_as_an_audit_trail(session, monkeypatch):
    from server import extractor

    cid, result = session
    html, plan = _plan_with_derived(cid, result["rows"], result["columns"])
    monkeypatch.setattr(extractor, "get_extractor", lambda: type("P", (), {"propose": lambda *_: plan})())

    first = _save(cid, html, "FF Audit")
    token = first["confirmation_token"]
    saved = _save(cid, html, "FF Audit", [{"token": token, "accept_all": True}])

    cached = extraction_cache.load(get_meta_connection(), token)
    assert cached["consumed_at"] and cached["report_id"] == saved["report_id"]
    assert cached["plan"]["derived_queries"], "the approved plan is still readable"


def test_two_proposals_of_the_same_plan_get_different_tokens(session, monkeypatch):
    """A token names a proposal, not a plan; otherwise 'already used' is meaningless."""
    from server import extractor

    cid, result = session
    html, plan = _plan_with_derived(cid, result["rows"], result["columns"])
    monkeypatch.setattr(extractor, "get_extractor", lambda: type("P", (), {"propose": lambda *_: plan})())

    first = _save(cid, html, "FF Two Proposals")
    second = _save(cid, html, "FF Two Proposals")
    assert first["confirmation_token"] != second["confirmation_token"]


def test_a_superseded_proposal_is_pruned(session, monkeypatch):
    """Re-proposing replaces the unconsumed plan instead of leaking a row."""
    from server import extractor

    cid, result = session
    html, plan = _plan_with_derived(cid, result["rows"], result["columns"])
    monkeypatch.setattr(extractor, "get_extractor", lambda: type("P", (), {"propose": lambda *_: plan})())

    _save(cid, html, "FF Prune")
    _save(cid, html, "FF Prune")
    _save(cid, html, "FF Prune")

    meta = get_meta_connection()
    rows = meta.execute(
        "SELECT COUNT(*) FROM extraction_plans WHERE conversation_id = ? AND consumed_at IS NULL",
        (cid,),
    ).fetchone()[0]
    assert rows == 1


def test_a_consumed_plan_is_not_pruned_by_a_later_proposal(session, monkeypatch):
    from server import extractor

    cid, result = session
    html, plan = _plan_with_derived(cid, result["rows"], result["columns"])
    monkeypatch.setattr(extractor, "get_extractor", lambda: type("P", (), {"propose": lambda *_: plan})())

    first = _save(cid, html, "FF Keep Audit")
    token = first["confirmation_token"]
    _save(cid, html, "FF Keep Audit", [{"token": token, "accept_all": True}])

    # A fresh proposal for the same conversation must not erase the approved one.
    _save(cid, html, "FF Keep Audit")
    assert extraction_cache.load(get_meta_connection(), token)["consumed_at"]


def test_a_page_edited_between_the_two_calls_is_refused(session, monkeypatch):
    """The plan is a set of offsets into the page it was proposed against."""
    from server import extractor

    cid, result = session
    html, plan = _plan_with_derived(cid, result["rows"], result["columns"])
    monkeypatch.setattr(extractor, "get_extractor", lambda: type("P", (), {"propose": lambda *_: plan})())

    first = _save(cid, html, "FF Stale")
    token = first["confirmation_token"]

    edited = html.replace("<div>", "<div><h1>A new heading that shifts every offset</h1>", 1)
    second = _save(cid, edited, "FF Stale", [{"token": token, "accept_all": True}])
    assert second["status"] == "structure_confirmation_stale"
    assert "the artifact changed" in second["error"]

    # The unedited page still confirms cleanly against the same token.
    third = _save(cid, html, "FF Stale", [{"token": token, "accept_all": True}])
    assert third["status"] == "registered", third


# ---------------------------------------------------------------------------
# Narrative overrides: watch and authored_as_of
# ---------------------------------------------------------------------------
#
# The whole chain -- plan schema, validate_plan, normalizer, artifact parser,
# parity, runner -- already supported editorial watches. Until now nothing could
# *put* one on a plan through the free-form flow.


def _forced_inference(cid, result, monkeypatch):
    """A free-form page whose plan holds an inference, so a token is issued."""
    from server import extractor

    html, plan = _plan_with_derived(cid, result["rows"], result["columns"])
    monkeypatch.setattr(
        extractor, "get_extractor", lambda: type("P", (), {"propose": lambda *_: plan})()
    )
    return html


def _first_admissions(result) -> int:
    return result["rows"][0]["admissions"]


def test_a_watch_override_lands_on_the_editorial_block(session, monkeypatch):
    cid, result = session
    html = _forced_inference(cid, result, monkeypatch)
    first = _save(cid, html, "FF Watch Lands")

    saved = _save(cid, html, "FF Watch Lands", [
        {"token": first["confirmation_token"], "accept_all": True},
        {
            "block_id": "b0",
            "tier": "editorial",
            "watch": "admissions_by_division[0].admissions > 99999",
            "authored_as_of": "2025-06-30",
        },
    ])
    assert saved["status"] == "registered", saved

    block = registry.get(get_meta_connection(), saved["report_id"])["editorial_blocks"][0]
    assert block["authored_as_of"] == "2025-06-30"
    assert block["watch"]["result"] == "admissions_by_division"
    assert block["watch"]["field"] == "admissions"
    assert block["watch"]["op"] == ">"
    assert block["watch"]["value"] == 99999.0


def test_a_watch_override_needs_no_tier_entry(session, monkeypatch):
    """An entry carrying only a watch must still apply (the block is already editorial)."""
    cid, result = session
    html = _forced_inference(cid, result, monkeypatch)
    first = _save(cid, html, "FF Watch Only")

    saved = _save(cid, html, "FF Watch Only", [
        {"token": first["confirmation_token"], "accept_all": True},
        {"block_id": "b0", "watch": "admissions_by_division[0].admissions > 1"},
    ])
    assert saved["status"] == "registered", saved
    assert registry.get(get_meta_connection(), saved["report_id"])["editorial_blocks"][0]["watch"]


def test_the_banner_fires_only_when_the_watch_condition_holds(session, monkeypatch, tmp_path):
    """Two reports, thresholds either side of the real number."""
    cid, result = session
    html = _forced_inference(cid, result, monkeypatch)
    actual = _first_admissions(result)

    ids = {}
    for label, watch in (
        ("fires", f"admissions_by_division[0].admissions > {actual - 1}"),
        ("silent", f"admissions_by_division[0].admissions > {actual + 1}"),
    ):
        name = f"FF Banner {label}"
        first = _save(cid, html, name)
        saved = _save(cid, html, name, [
            {"token": first["confirmation_token"], "accept_all": True},
            {"block_id": "b0", "tier": "editorial", "watch": watch},
        ])
        assert saved["status"] == "registered", saved
        ids[label] = saved["report_id"]

    fired = regenerate.regenerate(ids["fires"], None, ANCHOR_DATE, tmp_path, ["html"])
    silent = regenerate.regenerate(ids["silent"], None, ANCHOR_DATE, tmp_path, ["html"])
    fired_html = next(p for p in fired if p.suffix == ".html").read_text(encoding="utf-8")
    silent_html = next(p for p in silent if p.suffix == ".html").read_text(encoding="utf-8")

    assert 'class="staleness-banner"' in fired_html
    assert 'class="staleness-banner"' not in silent_html


def test_bad_watch_grammar_fails_the_call_and_leaves_the_token_usable(session, monkeypatch):
    """An override is a human decision: reject it loudly, do not silently drop it."""
    cid, result = session
    html = _forced_inference(cid, result, monkeypatch)
    first = _save(cid, html, "FF Bad Watch")
    token = first["confirmation_token"]

    bad = _save(cid, html, "FF Bad Watch", [
        {"token": token, "accept_all": True},
        {"block_id": "b0", "watch": "admissions_by_division[0].admissions <"},
    ])
    assert bad["status"] == "invalid_structure_confirmation"
    assert "b0" in bad["error"]
    assert extraction_cache.load(get_meta_connection(), token)["consumed_at"] is None

    corrected = _save(cid, html, "FF Bad Watch", [
        {"token": token, "accept_all": True},
        {"block_id": "b0", "watch": "admissions_by_division[0].admissions < 99999"},
    ])
    assert corrected["status"] == "registered", corrected


def test_a_watch_on_a_non_editorial_block_is_refused(session, monkeypatch):
    cid, result = session
    html = _forced_inference(cid, result, monkeypatch)
    first = _save(cid, html, "FF Watch Tier")

    refused = _save(cid, html, "FF Watch Tier", [
        {"token": first["confirmation_token"], "accept_all": True},
        {
            "block_id": "b0",
            "tier": "analytical",
            "goal": "summarize",
            "watch": "admissions_by_division[0].admissions > 1",
        },
    ])
    assert refused["status"] == "invalid_structure_confirmation"
    assert "requires tier 'editorial'" in refused["error"]
    with pytest.raises(KeyError):
        registry.get(get_meta_connection(), "ff_watch_tier")


def test_an_unknown_block_id_is_refused(session, monkeypatch):
    """Today a typo'd block_id was silently ignored. It is a human decision; say so."""
    cid, result = session
    html = _forced_inference(cid, result, monkeypatch)
    first = _save(cid, html, "FF Bad Block")

    refused = _save(cid, html, "FF Bad Block", [
        {"token": first["confirmation_token"], "accept_all": True},
        {"block_id": "b99", "tier": "editorial"},
    ])
    assert refused["status"] == "invalid_structure_confirmation"
    assert "unknown block_id 'b99'" in refused["error"]


def test_a_non_iso_authored_as_of_is_refused(session, monkeypatch):
    cid, result = session
    html = _forced_inference(cid, result, monkeypatch)
    first = _save(cid, html, "FF Bad Date")

    refused = _save(cid, html, "FF Bad Date", [
        {"token": first["confirmation_token"], "accept_all": True},
        {"block_id": "b0", "authored_as_of": "June 30, 2025"},
    ])
    assert refused["status"] == "invalid_structure_confirmation"
    assert "is not an ISO date" in refused["error"]


def test_a_clean_page_takes_a_watch_without_a_token(session):
    """No inference means no round-trip -- but a watch is a directive, not an inference."""
    cid, result = session
    html = _free_form_page(result["rows"], result["columns"])

    saved = _save(cid, html, "FF Clean Watch", [
        {
            "block_id": "b0",
            "tier": "editorial",
            "watch": "admissions_by_division[0].admissions > 99999",
            "authored_as_of": "2025-06-30",
        },
    ])
    assert saved["status"] == "registered", saved

    block = registry.get(get_meta_connection(), saved["report_id"])["editorial_blocks"][0]
    assert block["watch"]["result"] == "admissions_by_division"
    assert block["watch"]["op"] == ">"
    assert block["authored_as_of"] == "2025-06-30"


def test_a_tokenless_override_still_validates_loudly(session):
    cid, result = session
    html = _free_form_page(result["rows"], result["columns"])
    refused = _save(cid, html, "FF Clean Bad", [{"block_id": "b9", "tier": "editorial"}])
    assert refused["status"] == "invalid_structure_confirmation"
    assert "unknown block_id 'b9'" in refused["error"]


def test_a_tokenless_override_is_refused_when_the_plan_holds_an_inference(session, monkeypatch):
    """Inference is exactly what a token exists to get a human to look at."""
    cid, result = session
    html = _forced_inference(cid, result, monkeypatch)
    refused = _save(cid, html, "FF Needs Token", [{"block_id": "b0", "tier": "editorial"}])
    assert refused["status"] == "invalid_structure_confirmation"
    assert "confirmation_token" in refused["error"]
    with pytest.raises(KeyError):
        registry.get(get_meta_connection(), "ff_needs_token")


def test_a_watch_against_an_unmapped_result_fails_at_the_parity_gate(session, monkeypatch):
    """Grammar is fine, so no new check is needed: reference completeness catches it."""
    cid, result = session
    html = _forced_inference(cid, result, monkeypatch)
    first = _save(cid, html, "FF Watch Unmapped")

    saved = _save(cid, html, "FF Watch Unmapped", [
        {"token": first["confirmation_token"], "accept_all": True},
        {"block_id": "b0", "watch": "nosuch[0].x < 1"},
    ])
    assert saved["status"] == "parity_failed"
    assert "nosuch" in saved["parity"]["diff_summary"]
    with pytest.raises(KeyError):
        registry.get(get_meta_connection(), "ff_watch_unmapped")


def test_an_unconsumed_token_survives_a_parity_failure(session, monkeypatch):
    """The plan was fine; the artifact was not. The client may retry the same plan."""
    from server import extractor, parity

    cid, result = session
    html, plan = _plan_with_derived(cid, result["rows"], result["columns"])
    monkeypatch.setattr(extractor, "get_extractor", lambda: type("P", (), {"propose": lambda *_: plan})())

    first = _save(cid, html, "FF Parity Retry")
    token = first["confirmation_token"]

    monkeypatch.setattr(
        parity, "check", lambda *a, **k: {"passed": False, "diff_summary": "forced failure"}
    )
    failed = _save(cid, html, "FF Parity Retry", [{"token": token, "accept_all": True}])
    assert failed["status"] == "parity_failed"
    assert extraction_cache.load(get_meta_connection(), token)["consumed_at"] is None

    monkeypatch.undo()
    monkeypatch.setattr(extractor, "get_extractor", lambda: type("P", (), {"propose": lambda *_: plan})())
    retried = _save(cid, html, "FF Parity Retry", [{"token": token, "accept_all": True}])
    assert retried["status"] == "registered", retried


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
