"""Result fingerprints (WS11-A) and the deterministic matcher (WS11-B).

Part 1 covers canonicalization, the sha256 fingerprint, the in-place SQLite
migration, and the execute_sql write path. Part 2 covers the tolerant JS literal
reader and blob/scalar matching.
"""

from __future__ import annotations

import datetime as dt
import decimal
import sqlite3

import pytest

from server import call_log, db, fingerprint, tools
from server.db import get_meta_connection
from server.fingerprint import NotALiteral, read_js_literal

# ---------------------------------------------------------------------------
# Part 1 -- canonicalization and fingerprints
# ---------------------------------------------------------------------------


def test_same_result_same_fingerprint():
    cols = ["division", "admissions"]
    rows = [{"division": "North", "admissions": 8415}]
    first, _ = call_log.fingerprint_result(cols, rows)
    second, _ = call_log.fingerprint_result(cols, [dict(rows[0])])
    assert first == second


def test_reordered_rows_change_the_fingerprint():
    cols = ["division", "admissions"]
    a = [{"division": "North", "admissions": 1}, {"division": "South", "admissions": 2}]
    b = list(reversed(a))
    assert call_log.fingerprint_result(cols, a)[0] != call_log.fingerprint_result(cols, b)[0]


def test_floats_agreeing_to_six_places_fingerprint_the_same():
    cols = ["rate"]
    a, _ = call_log.fingerprint_result(cols, [{"rate": 0.5000001}])
    b, _ = call_log.fingerprint_result(cols, [{"rate": 0.5}])
    assert a == b


def test_floats_differing_at_six_places_do_not():
    cols = ["rate"]
    a, _ = call_log.fingerprint_result(cols, [{"rate": 0.500001}])
    b, _ = call_log.fingerprint_result(cols, [{"rate": 0.5}])
    assert a != b


def test_booleans_are_preserved_not_collapsed_into_numbers():
    """`is_hca` is a real boolean column; True must not fingerprint as 1."""
    assert call_log.canonical_scalar(True) is True
    assert call_log.canonical_scalar(False) is False
    cols = ["is_hca"]
    truthy, _ = call_log.fingerprint_result(cols, [{"is_hca": True}])
    one, _ = call_log.fingerprint_result(cols, [{"is_hca": 1}])
    assert truthy != one


def test_none_survives_canonicalization():
    assert call_log.canonical_scalar(None) is None


def test_duckdb_scalar_types_canonicalize_like_an_island():
    """Dates isoformat and Decimals become floats, matching render._json_default."""
    assert call_log.canonical_scalar(dt.date(2025, 6, 30)) == "2025-06-30"
    assert call_log.canonical_scalar(decimal.Decimal("12.3456789")) == 12.345679


def test_text_canonicalization_undoes_the_display_filters():
    assert call_log.canonical_scalar("+1,234", from_text=True) == 1234.0
    assert call_log.canonical_scalar("34.5%", from_text=True) == 34.5
    assert call_log.canonical_scalar("-2.7pp", from_text=True) == -2.7
    assert call_log.canonical_scalar("  ORTHOPEDICS ", from_text=True) == "ORTHOPEDICS"


def test_a_js_literal_an_html_cell_and_a_duckdb_int_hash_equal():
    """The whole point: one canonical scalar set across all three sources."""
    cols = ["cases"]
    typed, _ = call_log.fingerprint_result(cols, [{"cases": 1234}])
    from_js, _ = call_log.fingerprint_result(cols, [{"cases": 1234.0}])
    assert typed == from_js
    assert call_log.canonical_scalar("1,234", from_text=True) == call_log.canonical_scalar(1234)


def test_oversized_result_keeps_the_fingerprint_and_drops_the_rows():
    cols = ["blob"]
    rows = [{"blob": "x" * 5000} for _ in range(100)]  # ~500 KB canonical
    fingerprint, canonical = call_log.fingerprint_result(cols, rows)
    assert fingerprint and canonical is None


def test_canonical_json_keeps_column_order_not_sorted_order():
    canonical = call_log.canonical_result(["z", "a"], [{"z": 1, "a": 2}])
    assert call_log.canonical_json(canonical).startswith('{"columns":["z","a"]')


# ---------------------------------------------------------------------------
# Part 1 -- storage: migration and the execute_sql write path
# ---------------------------------------------------------------------------

_PRE_WS11_SCHEMA = """
CREATE TABLE tool_call_log (
  call_id         INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT,
  tool_name       TEXT,
  sql_text        TEXT,
  result_name     TEXT,
  row_count       INTEGER,
  called_at       TEXT
);
"""


def test_migration_adds_columns_to_a_pre_ws11_table(tmp_path):
    """CREATE TABLE IF NOT EXISTS never alters an existing table; ALTER must."""
    path = tmp_path / "old_meta.sqlite"
    con = sqlite3.connect(str(path))
    con.executescript(_PRE_WS11_SCHEMA)
    con.execute(
        "INSERT INTO tool_call_log (conversation_id, tool_name, sql_text, "
        "result_name, row_count, called_at) VALUES ('c', 'execute_sql', 'SELECT 1', 'r', 1, 'now')"
    )
    con.commit()
    before = {row[1] for row in con.execute("PRAGMA table_info(tool_call_log)")}
    assert "result_fingerprint" not in before

    db._migrate_meta_store(con)
    con.commit()

    after = {row[1] for row in con.execute("PRAGMA table_info(tool_call_log)")}
    assert {"result_fingerprint", "result_rows"} <= after
    # The pre-existing row survives, with NULLs in the new columns.
    row = con.execute("SELECT result_fingerprint, result_rows FROM tool_call_log").fetchone()
    assert row == (None, None)
    con.close()


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "meta.sqlite"
    con = sqlite3.connect(str(path))
    con.executescript(_PRE_WS11_SCHEMA)
    db._migrate_meta_store(con)
    db._migrate_meta_store(con)  # must not raise "duplicate column name"
    con.close()


def test_migration_skips_tables_that_do_not_exist_yet(tmp_path):
    """A store with tool_call_log but no extraction_plans must migrate cleanly."""
    con = sqlite3.connect(str(tmp_path / "partial.sqlite"))
    con.executescript(_PRE_WS11_SCHEMA)
    db._migrate_meta_store(con)  # extraction_plans absent; must not raise
    con.close()


_PRE_CONSUME_PLANS = """
CREATE TABLE extraction_plans (
  token           TEXT PRIMARY KEY,
  conversation_id TEXT,
  plan_json       TEXT,
  created_at      TEXT
);
"""


def test_migration_adds_the_plan_consumption_columns(tmp_path):
    con = sqlite3.connect(str(tmp_path / "plans.sqlite"))
    con.executescript(_PRE_WS11_SCHEMA + _PRE_CONSUME_PLANS)
    con.execute(
        "INSERT INTO extraction_plans VALUES ('tok', 'cid', '{}', 'now')"
    )
    con.commit()

    db._migrate_meta_store(con)
    con.commit()

    cols = {row[1] for row in con.execute("PRAGMA table_info(extraction_plans)")}
    assert {"consumed_at", "report_id"} <= cols
    # The pre-existing plan survives, unconsumed.
    assert con.execute("SELECT consumed_at, report_id FROM extraction_plans").fetchone() == (
        None,
        None,
    )
    con.close()


def test_log_call_is_still_callable_with_six_positional_args():
    """The new columns are trailing and optional; existing call sites are untouched."""
    meta = get_meta_connection()
    call_id = call_log.log_call(meta, "fp-positional", "execute_sql", "SELECT 1", "r", 1)
    assert call_id > 0
    fetched = call_log.fetch(meta, "fp-positional")
    assert fetched[0]["result_fingerprint"] is None
    assert fetched[0]["result_rows"] is None


def test_execute_sql_writes_the_fingerprint_and_the_rows():
    cid = "fp-execute"
    result = tools.execute_sql(cid, "SELECT 1 AS n", "one")
    assert result["row_count"] == 1

    call = call_log.fetch(get_meta_connection(), cid)[-1]
    assert call["tool_name"] == "execute_sql"
    assert call["result_fingerprint"]
    # fetch() parses the JSON blob back into {columns, rows} for consumers.
    assert call["result_rows"] == {"columns": ["n"], "rows": [[1.0]]}

    expected, _ = call_log.fingerprint_result(result["columns"], result["rows"])
    assert call["result_fingerprint"] == expected


def test_execute_logged_records_the_tool_name_for_derived_queries():
    cid = "fp-derive"
    tools.execute_logged(cid, "SELECT 2 AS n", "two", "save_derive")
    call = call_log.fetch(get_meta_connection(), cid)[-1]
    assert call["tool_name"] == "save_derive"
    assert call["result_fingerprint"]


def test_fetch_tolerates_a_corrupt_rows_blob():
    meta = get_meta_connection()
    call_log.log_call(meta, "fp-corrupt", "execute_sql", "SELECT 1", "r", 1, "abc", "{not json")
    assert call_log.fetch(meta, "fp-corrupt")[0]["result_rows"] is None


# ---------------------------------------------------------------------------
# Part 2 -- the tolerant JS literal reader
# ---------------------------------------------------------------------------
#
# Fixtures in the shape a hand-built dashboard actually writes: unquoted keys,
# single quotes, trailing commas. The original Las Vegas artifact these mimic was
# never committed to this repo, so they are synthesized from real query results.

_RACE_JS = """[
  {qtr: 'Q2\\'21', hca: 4200, uhs: 7529, gap: 3329},
  {qtr: 'Q3\\'21', hca: 4310, uhs: 7501, gap: 3191},
  {qtr: 'Q1\\'25', hca: 6120, uhs: 6782, gap: 662},
]"""

_ESL_QTR_JS = """{
  ORTHOPEDICS: [{qtr: 'Q2\\'23', share_pct: 34.8}, {qtr: 'Q1\\'25', share_pct: 30.0}],
  'GENERAL SURGERY': [{qtr: 'Q2\\'23', share_pct: 23.4}, {qtr: 'Q1\\'25', share_pct: 25.7}],
}"""


def test_reader_round_trips_a_race_style_constant():
    value = read_js_literal(_RACE_JS)
    assert len(value) == 3
    assert value[0] == {"qtr": "Q2'21", "hca": 4200, "uhs": 7529, "gap": 3329}
    assert value[-1]["gap"] == 662


def test_reader_round_trips_a_grouped_dict_constant():
    value = read_js_literal(_ESL_QTR_JS)
    assert set(value) == {"ORTHOPEDICS", "GENERAL SURGERY"}
    assert value["ORTHOPEDICS"][1] == {"qtr": "Q1'25", "share_pct": 30.0}


@pytest.mark.parametrize(
    "source",
    [
        "[1, 2.5, -3, 1e3, 1.5e-2]",
        "{a: null, b: true, c: false}",
        '{"quoted": 1, unquoted: 2}',
        "[{a: 1},]",  # trailing comma
        "{nested: [{deep: [1, 2]}]}",
    ],
)
def test_reader_accepts_the_literal_subset(source):
    read_js_literal(source)


@pytest.mark.parametrize(
    "source, why",
    [
        ("[1 + 2]", "arithmetic"),
        ("[foo()]", "a function call"),
        ("[d.uhs - d.hca]", "a member expression"),
        ("{gap: TOTAL}", "an identifier reference"),
        ("[a ? b : c]", "a ternary"),
        ("[1, 2] .map(x => x)", "trailing content"),
        ("{x: 1 * 2}", "multiplication"),
        ("[Math.max(1, 2)]", "a namespaced call"),
    ],
)
def test_reader_rejects_anything_that_computes(source, why):
    with pytest.raises(NotALiteral):
        read_js_literal(source)


# ---------------------------------------------------------------------------
# Part 2 -- blob and scalar matching
# ---------------------------------------------------------------------------

_RACE_COLUMNS = ["qtr", "hca", "uhs", "gap"]
_RACE_ROWS = [
    {"qtr": "Q2'21", "hca": 4200, "uhs": 7529, "gap": 3329},
    {"qtr": "Q3'21", "hca": 4310, "uhs": 7501, "gap": 3191},
    {"qtr": "Q1'25", "hca": 6120, "uhs": 6782, "gap": 662},
]

_ESL_COLUMNS = ["esl", "qtr", "share_pct"]
_ESL_ROWS = [
    {"esl": "ORTHOPEDICS", "qtr": "Q2'23", "share_pct": 34.8},
    {"esl": "ORTHOPEDICS", "qtr": "Q1'25", "share_pct": 30.0},
    {"esl": "GENERAL SURGERY", "qtr": "Q2'23", "share_pct": 23.4},
    {"esl": "GENERAL SURGERY", "qtr": "Q1'25", "share_pct": 25.7},
]

_KPI_COLUMNS = ["gap_now", "hca_share_pct", "ortho_change_pp"]
_KPI_ROWS = [{"gap_now": 662, "hca_share_pct": 35.3, "ortho_change_pp": -2.7}]


def _call(result_name, columns, rows):
    fingerprint_hex, canonical = call_log.fingerprint_result(columns, rows)
    payload = call_log.canonical_result(columns, rows)
    return {
        "result_name": result_name,
        "sql_text": f"SELECT * FROM {result_name}",
        "tool_name": "execute_sql",
        "result_fingerprint": fingerprint_hex,
        "result_rows": payload if canonical else None,
    }


@pytest.fixture
def calls():
    return [
        _call("race_quarters", _RACE_COLUMNS, _RACE_ROWS),
        _call("esl_quarters", _ESL_COLUMNS, _ESL_ROWS),
        _call("kpi_summary", _KPI_COLUMNS, _KPI_ROWS),
    ]


def _script(body):
    return f"<div><script>{body}</script></div>"


def test_a_constant_regenerated_verbatim_matches_its_result(calls):
    report = fingerprint.match(_script(f"const RACE = {_RACE_JS};"), calls)
    assert not report.unmatched
    assert [(m.blob_id, m.result_name, m.match_type) for m in report.matches] == [
        ("const:RACE", "race_quarters", "rowset")
    ]


def test_a_projection_of_the_result_columns_matches(calls):
    """A JS constant often carries a subset of the query's columns."""
    projected = "[{qtr: 'Q2\\'21', gap: 3329}, {qtr: 'Q3\\'21', gap: 3191}, {qtr: 'Q1\\'25', gap: 662}]"
    report = fingerprint.match(_script(f"const RACE = {projected};"), calls)
    match_ = report.matches[0]
    assert match_.match_type == "projection"
    assert match_.result_name == "race_quarters"
    assert match_.columns == ("qtr", "gap")


def test_a_grouped_dict_matches_its_long_form_result(calls):
    report = fingerprint.match(_script(f"const ESL_QTR = {_ESL_QTR_JS};"), calls)
    match_ = report.matches[0]
    assert match_.match_type == "grouped"
    assert match_.result_name == "esl_quarters"
    assert match_.grouped_by == "esl"


def test_one_edited_cell_does_not_match(calls):
    tampered = _RACE_JS.replace("gap: 662", "gap: 999")
    report = fingerprint.match(_script(f"const RACE = {tampered};"), calls)
    assert not report.matches
    assert [b.blob_id for b in report.unmatched] == ["const:RACE"]


def test_a_computed_constant_is_unparseable_not_matched(calls):
    """`gap: d.uhs - d.hca` is not data; it is a derivation the page performed."""
    computed = "const GAPS = [{qtr: 'Q1\\'25', gap: d.uhs - d.hca}];"
    report = fingerprint.match(_script(computed), calls)
    assert report.unparseable == ["GAPS"]
    assert not report.matches


def test_local_variables_are_not_mistaken_for_data(calls):
    """A drawing function's scratch variables are code. Reporting them is noise."""
    body = (
        "var w = 720, h = 260;\n"
        "var svg = document.getElementById('chart');\n"
        "var pts = function (d) { return d.x + ',' + d.y; };\n"
        "const LABEL = 'Race to #1';\n"
    )
    report = fingerprint.match(_script(body), calls)
    assert report.unparseable == []
    assert report.blobs == []


def test_a_kpi_scalar_resolves_to_a_single_row_result(calls):
    """35.3 appears only in kpi_summary, so it binds unambiguously to row 0."""
    html = "<p>HCA holds <strong>35.3</strong> of the market.</p>"
    report = fingerprint.match(html, calls)
    ref = report.value_matches[0].ref
    assert (ref.result, ref.field, ref.selector) == (
        "kpi_summary",
        "hca_share_pct",
        ("index", 0),
    )


def test_a_scalar_with_display_filters_still_resolves(calls):
    html = "<p>Share is <strong>35.3%</strong> and ortho moved <b>-2.7pp</b>.</p>"
    report = fingerprint.match(html, calls)
    fields = {m.ref.field for m in report.value_matches}
    assert fields == {"hca_share_pct", "ortho_change_pp"}


def test_a_unique_cell_in_a_multi_row_result_gets_a_key_selector(calls):
    """34.8 appears once, in the ORTHOPEDICS/Q2'23 row -> [col='key'] selector."""
    report = fingerprint.match("<p><strong>34.8</strong></p>", calls)
    ref = report.value_matches[0].ref
    assert ref.result == "esl_quarters"
    assert ref.field == "share_pct"
    assert ref.selector == ("match", "esl", "ORTHOPEDICS")


def test_an_ambiguous_scalar_records_every_candidate(calls):
    """662 is both kpi_summary.gap_now and race_quarters' last gap."""
    report = fingerprint.match("<p><strong>662</strong></p>", calls)
    assert not report.value_matches
    scalar, candidates = report.ambiguous_values[0]
    assert scalar.raw_text == "662"
    assert {c.result_name for c in candidates} == {"kpi_summary", "race_quarters"}


def test_a_number_matching_nothing_is_unresolved(calls):
    report = fingerprint.match("<p><strong>123456</strong></p>", calls)
    assert not report.value_matches
    assert [s.raw_text for s in report.unresolved_values] == ["123456"]


def test_numbers_inside_a_matched_blob_are_not_also_scalar_candidates(calls):
    """The island already carries them; binding them twice would break parity counts."""
    report = fingerprint.match(_script(f"const RACE = {_RACE_JS};"), calls)
    assert not report.value_matches and not report.unresolved_values


def test_a_populated_html_table_matches_a_result(calls):
    rows = "".join(
        f"<tr><td>{r['qtr']}</td><td>{r['hca']}</td><td>{r['uhs']}</td><td>{r['gap']}</td></tr>"
        for r in _RACE_ROWS
    )
    html = (
        "<table><thead><tr><th>qtr</th><th>hca</th><th>uhs</th><th>gap</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )
    report = fingerprint.match(html, calls)
    assert report.matches[0].result_name == "race_quarters"
    assert report.matches[0].match_type == "rowset"


def test_booleans_match_a_duckdb_boolean_column():
    calls_ = [_call("systems", ["health_system", "is_hca"], [{"health_system": "HCA", "is_hca": True}])]
    js = "const SYS = [{health_system: 'HCA', is_hca: true}];"
    report = fingerprint.match(_script(js), calls_)
    assert report.matches[0].result_name == "systems"


def test_an_over_cap_result_still_takes_an_exact_rowset_match():
    call = _call("race_quarters", _RACE_COLUMNS, _RACE_ROWS)
    call["result_rows"] = None  # simulate exceeding the 256 KB cap
    report = fingerprint.match(_script(f"const RACE = {_RACE_JS};"), [call])
    assert report.matches[0].match_type == "rowset"
    assert any("exceeded the row cap" in note for note in report.notes)


def test_an_over_cap_result_cannot_take_a_projection_match():
    call = _call("race_quarters", _RACE_COLUMNS, _RACE_ROWS)
    call["result_rows"] = None
    projected = "[{qtr: 'Q2\\'21', gap: 3329}, {qtr: 'Q3\\'21', gap: 3191}, {qtr: 'Q1\\'25', gap: 662}]"
    report = fingerprint.match(_script(f"const RACE = {projected};"), [call])
    assert not report.matches and report.unmatched
