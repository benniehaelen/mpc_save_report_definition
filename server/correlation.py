"""Resolve the lineage correlation key for a tool call.

Every tool call is keyed by a correlation id so the distiller can gather the
queries a report was built from. Historically that id was an explicit
``conversation_id`` argument, which made the *model* responsible for using one
consistent string across a whole session -- an instruction it can botch (a fresh
id per call, a typo, an id lost after context summarization), surfacing much
later as a save that finds zero logged queries.

A well-behaved MCP client already carries a stable per-chat id in the request's
``_meta`` header (VS Code Copilot sends ``vscode.conversationId``). This module
resolves the key from a fallback chain so correlation is automatic when the
caller does not pass one, while the explicit argument stays the contract for
non-Copilot clients, the demo scripts, and the tests.

The rule that matters: **``_meta`` is a *source* of the key, not the key.**
Resolution is explicit -> ``_meta`` -> generated, and the meta-/gen- prefixes keep
a resolved key from ever colliding with an explicitly-passed ``conversation_id``.
"""

from __future__ import annotations

import os
import uuid

# The client `_meta` field(s) to read, most-preferred first. A comma list, read
# at call time so tests can override it. Default: the id VS Code Copilot sends,
# documented as stable for the whole chat conversation. Its `_meta` siblings are
# NOT chat-stable -- `vscode.requestId` is per turn, `progressToken` per
# operation, `traceparent` per trace -- so the default list is exactly this one.
_DEFAULT_META_KEYS = "vscode.conversationId"


def _configured_keys() -> list[str]:
    raw = os.environ.get("POC_CORRELATION_META_KEYS", _DEFAULT_META_KEYS)
    return [key.strip() for key in raw.split(",") if key.strip()]


def resolve(explicit: str | None, meta: dict | None) -> tuple[str, str]:
    """Return ``(correlation_key, source)``; source in explicit|meta|generated.

    A non-empty ``explicit`` wins, always, returned verbatim. Otherwise the first
    non-empty value among the configured ``_meta`` keys becomes ``meta-<value>``.
    Otherwise a fresh ``gen-<uuid4>`` is minted -- correlation could not be
    established, and the caller is expected to warn.

    Production note (not implemented here): on a shared, multi-tenant server the
    key must be namespaced by the authenticated principal --
    ``key = f"{principal}:{value}"`` -- because a client-supplied id is a claim,
    not an identity. The id correlates; authentication authorizes.
    """
    if explicit and explicit.strip():
        return explicit, "explicit"

    if meta:
        for key in _configured_keys():
            value = meta.get(key)
            if value and str(value).strip():
                return f"meta-{value}", "meta"

    return f"gen-{uuid.uuid4()}", "generated"
