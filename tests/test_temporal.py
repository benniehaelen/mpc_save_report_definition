"""Unit tests for temporal re-parameterization, the riskiest piece."""

from __future__ import annotations

from server import temporal

ANCHOR = "2025-06-30"


def test_detects_and_rewrites_relative_literal():
    sql = "WHERE admit_date >= DATE '2025-06-01'"
    out, warnings = temporal.reparameterize(sql, ANCHOR)
    assert "__REPORT_DATE__ - INTERVAL 29 DAY" in out
    assert "DATE '2025-06-01'" not in out
    assert warnings == []


def test_full_window_example_from_brief():
    sql = (
        "admit_date >= DATE '2025-06-01' "
        "AND admit_date < DATE '2025-07-01'"
    )
    out, _ = temporal.reparameterize(sql, ANCHOR)
    assert out == (
        "admit_date >= __REPORT_DATE__ - INTERVAL 29 DAY "
        "AND admit_date < __REPORT_DATE__ + INTERVAL 1 DAY"
    )


def test_anchor_itself_becomes_bare_token():
    sql = "WHERE census_date = DATE '2025-06-30'"
    out, _ = temporal.reparameterize(sql, ANCHOR)
    assert "= __REPORT_DATE__" in out
    assert "INTERVAL" not in out


def test_bare_quoted_date_is_handled():
    sql = "WHERE admit_date >= '2025-06-15'"
    out, _ = temporal.reparameterize(sql, ANCHOR)
    assert "__REPORT_DATE__ - INTERVAL 15 DAY" in out


def test_fixed_confirmation_passthrough():
    sql = "WHERE admit_date >= DATE '2025-06-01'"
    confirmations = [{"literal": "2025-06-01", "treatment": "fixed"}]
    out, warnings = temporal.reparameterize(sql, ANCHOR, confirmations)
    assert "DATE '2025-06-01'" in out
    assert "__REPORT_DATE__" not in out
    assert any("fixed" in w for w in warnings)


def test_conservative_fallback_for_distant_literal():
    sql = "WHERE admit_date >= DATE '2020-01-01'"
    out, warnings = temporal.reparameterize(sql, ANCHOR)
    assert "DATE '2020-01-01'" in out
    assert "__REPORT_DATE__" not in out
    assert any("left fixed" in w for w in warnings)


def test_bind_report_date_substitutes_token():
    sql = "admit_date >= __REPORT_DATE__ - INTERVAL 29 DAY"
    bound = temporal.bind_report_date(sql, "2025-05-15")
    assert bound == "admit_date >= DATE '2025-05-15' - INTERVAL 29 DAY"
