"""Correlation-key resolution: explicit -> _meta -> generated."""

from __future__ import annotations

from server import correlation

_VSCODE = "vscode.conversationId"


def test_explicit_wins_over_meta():
    key, source = correlation.resolve("my-session", {_VSCODE: "abc"})
    assert (key, source) == ("my-session", "explicit")


def test_explicit_is_returned_verbatim_without_a_prefix():
    key, source = correlation.resolve("plain-id", None)
    assert key == "plain-id" and source == "explicit"


def test_a_meta_value_is_prefixed_meta():
    key, source = correlation.resolve(None, {_VSCODE: "4bbaa913-a8a3"})
    assert (key, source) == ("meta-4bbaa913-a8a3", "meta")


def test_the_literal_dotted_key_resolves():
    """vscode.conversationId is one map key, not meta['vscode']['conversationId']."""
    key, source = correlation.resolve(None, {"vscode.conversationId": "x"})
    assert source == "meta" and key == "meta-x"


def test_no_meta_generates_with_a_gen_prefix():
    key, source = correlation.resolve(None, None)
    assert source == "generated" and key.startswith("gen-")


def test_empty_meta_generates():
    key, source = correlation.resolve(None, {})
    assert source == "generated"


def test_generated_keys_are_unique():
    a, _ = correlation.resolve(None, None)
    b, _ = correlation.resolve(None, None)
    assert a != b


def test_an_empty_explicit_string_does_not_win():
    key, source = correlation.resolve("   ", {_VSCODE: "abc"})
    assert (key, source) == ("meta-abc", "meta")


def test_an_empty_meta_value_falls_through_to_generated():
    key, source = correlation.resolve(None, {_VSCODE: "  "})
    assert source == "generated"


def test_meta_without_the_configured_key_generates():
    key, source = correlation.resolve(None, {"vscode.requestId": "per-turn"})
    assert source == "generated"


def test_keys_are_probed_in_configured_order(monkeypatch):
    monkeypatch.setenv("POC_CORRELATION_META_KEYS", "first.key, second.key")
    key, source = correlation.resolve(None, {"second.key": "b", "first.key": "a"})
    assert key == "meta-a"  # first.key wins even though both are present


def test_a_later_configured_key_is_used_when_the_first_is_absent(monkeypatch):
    monkeypatch.setenv("POC_CORRELATION_META_KEYS", "first.key, second.key")
    key, source = correlation.resolve(None, {"second.key": "b"})
    assert key == "meta-b"


def test_the_env_override_can_point_at_a_different_client_field(monkeypatch):
    monkeypatch.setenv("POC_CORRELATION_META_KEYS", "traceId")
    key, source = correlation.resolve(None, {"traceId": "t-1", _VSCODE: "ignored"})
    assert key == "meta-t-1"


def test_an_empty_env_override_derives_from_nothing(monkeypatch):
    """A blank key list means no field is trusted; everything generates."""
    monkeypatch.setenv("POC_CORRELATION_META_KEYS", "  ,  ")
    key, source = correlation.resolve(None, {_VSCODE: "abc"})
    assert source == "generated"


def test_the_default_field_is_vscode_conversation_id(monkeypatch):
    monkeypatch.delenv("POC_CORRELATION_META_KEYS", raising=False)
    _, source = correlation.resolve(None, {"vscode.conversationId": "abc"})
    assert source == "meta"
