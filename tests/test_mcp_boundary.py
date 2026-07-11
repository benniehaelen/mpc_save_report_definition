"""The MCP boundary: correlation resolution through the real FastMCP wrappers.

Unlike the rest of the suite (which calls `server.tools.*` directly), these drive
`server/main.py` end to end via an in-process `fastmcp.Client`, injecting a fake
`_meta` header the way VS Code Copilot would. That is the only path that exercises
`conversation_id` resolution, the `session` block, and the generated warning.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client

from server.main import mcp

_ADMISSIONS = (
    "SELECT f.division, COUNT(*) AS admissions "
    "FROM admissions a JOIN facilities f ON a.facility_id = f.facility_id "
    "WHERE a.admit_date >= DATE '2025-06-01' AND a.admit_date < DATE '2025-07-01' "
    "GROUP BY f.division ORDER BY f.division"
)


async def _call(tool: str, args: dict, meta: dict | None = None) -> dict:
    async with Client(mcp) as client:
        result = await client.call_tool(tool, args, meta=meta)
    return result.data


def _run(coro):
    return asyncio.run(coro)


def _artifact(rows, columns):
    from runner import render

    html = render.build_table_html("admissions_by_division", columns, rows)
    return {"format": "html", "title": "Boundary", "content": html}


# ---------------------------------------------------------------------------
# _meta correlation: calls from one chat land under one key
# ---------------------------------------------------------------------------


def test_meta_correlates_calls_without_a_conversation_id(request):
    """Two execute_sql calls with the same _meta, no conversation_id, correlate."""
    conv = f"vscode-{request.node.name}"

    async def scenario():
        first = await _call(
            "execute_sql",
            {"sql": _ADMISSIONS, "result_name": "admissions_by_division"},
            meta={"vscode.conversationId": conv},
        )
        second = await _call(
            "execute_sql",
            {"sql": "SELECT 0.72 AS occupancy_rate", "result_name": "overall_occupancy"},
            meta={"vscode.conversationId": conv},
        )
        saved = await _call(
            "save_report_definition",
            {
                "report_name": f"Boundary {request.node.name}",
                "transcript": [],
                "final_artifact": _artifact(first["rows"], first["columns"]),
            },
            meta={"vscode.conversationId": conv},
        )
        return first, second, saved

    first, second, saved = _run(scenario())
    assert first["row_count"] > 0 and second["row_count"] == 1
    assert saved["status"] == "registered", saved
    # Both queries were found under the one meta-derived key.
    assert saved["session"]["source"] == "meta"
    assert saved["session"]["logged_calls"] == 2
    assert saved["session"]["correlation_key"].startswith("meta-")


def test_the_session_block_is_always_present_on_a_save(request):
    conv = f"vscode-{request.node.name}"

    async def scenario():
        await _call(
            "execute_sql",
            {"sql": _ADMISSIONS, "result_name": "admissions_by_division"},
            meta={"vscode.conversationId": conv},
        )
        return await _call(
            "save_report_definition",
            {
                "report_name": f"Session Block {request.node.name}",
                "transcript": [],
                "final_artifact": _artifact(
                    [{"division": "North", "admissions": 1}], ["division", "admissions"]
                ),
            },
            meta={"vscode.conversationId": conv},
        )

    saved = _run(scenario())
    assert set(saved["session"]) == {"correlation_key", "source", "logged_calls"}


# ---------------------------------------------------------------------------
# Explicit still wins, end to end
# ---------------------------------------------------------------------------


def test_an_explicit_conversation_id_beats_the_meta_header(request):
    explicit = f"explicit-{request.node.name}"

    async def scenario():
        adm = await _call(
            "execute_sql",
            {
                "sql": _ADMISSIONS,
                "result_name": "admissions_by_division",
                "conversation_id": explicit,
            },
            meta={"vscode.conversationId": "a-different-chat"},
        )
        return await _call(
            "save_report_definition",
            {
                "report_name": f"Explicit Wins {request.node.name}",
                "transcript": [],
                "final_artifact": _artifact(adm["rows"], adm["columns"]),
                "conversation_id": explicit,
            },
            meta={"vscode.conversationId": "a-different-chat"},
        )

    saved = _run(scenario())
    assert saved["status"] == "registered", saved
    assert saved["session"]["source"] == "explicit"
    # No prefix on an explicit key.
    assert saved["session"]["correlation_key"].startswith(explicit[:12])


# ---------------------------------------------------------------------------
# No meta at all: generated key, loud warning, safe failure
# ---------------------------------------------------------------------------


def test_no_meta_and_no_id_generates_a_key_and_warns():
    result = _run(
        _call("execute_sql", {"sql": "SELECT 1 AS n", "result_name": "probe"})
    )
    # The query still runs; correlation just isn't established.
    assert result["row_count"] == 1
    assert any("correlation is not established" in w for w in result.get("warnings", []))


def test_a_save_with_no_logged_calls_gets_the_targeted_remedy(request):
    """A generated key has nothing under it -> a clear message, not parity_failed."""
    saved = _run(
        _call(
            "save_report_definition",
            {
                "report_name": f"Empty {request.node.name}",
                "transcript": [],
                "final_artifact": _artifact(
                    [{"division": "North", "admissions": 1}], ["division", "admissions"]
                ),
            },
        )
    )
    assert saved["status"] == "no_logged_calls"
    assert saved["report_id"] is None
    assert "0 executed queries found" in saved["error"]
    assert "re-run the execute_sql calls" in saved["error"]
    assert saved["session"]["source"] == "generated"
    assert saved["session"]["logged_calls"] == 0
    assert any("correlation is not established" in w for w in saved.get("warnings", []))


def test_a_rotated_meta_id_fails_with_the_remedy_not_a_mystery(request):
    """Queries under one chat id, save under another (a window-reload rotation)."""
    async def scenario():
        await _call(
            "execute_sql",
            {"sql": _ADMISSIONS, "result_name": "admissions_by_division"},
            meta={"vscode.conversationId": f"chat-A-{request.node.name}"},
        )
        return await _call(
            "save_report_definition",
            {
                "report_name": f"Rotated {request.node.name}",
                "transcript": [],
                "final_artifact": _artifact(
                    [{"division": "North", "admissions": 1}], ["division", "admissions"]
                ),
            },
            meta={"vscode.conversationId": f"chat-B-{request.node.name}"},
        )

    saved = _run(scenario())
    assert saved["status"] == "no_logged_calls"
    assert "chat-B" in saved["error"] or saved["session"]["correlation_key"].startswith("meta-")
    assert saved["session"]["logged_calls"] == 0


# ---------------------------------------------------------------------------
# dry_run_sql resolves too (uniform), and logs nothing
# ---------------------------------------------------------------------------


def test_dry_run_resolves_without_logging(request):
    conv = f"vscode-{request.node.name}"
    result = _run(
        _call("dry_run_sql", {"sql": "SELECT 1 AS n"}, meta={"vscode.conversationId": conv})
    )
    assert result["valid"] is True
    # It resolved a key but logged nothing, so a later save finds zero calls.
    saved = _run(
        _call(
            "save_report_definition",
            {
                "report_name": f"Dry {request.node.name}",
                "transcript": [],
                "final_artifact": _artifact(
                    [{"division": "North", "admissions": 1}], ["division", "admissions"]
                ),
            },
            meta={"vscode.conversationId": conv},
        )
    )
    assert saved["status"] == "no_logged_calls"
