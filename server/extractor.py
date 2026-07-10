"""Propose how to normalize a free-form artifact into the v2 contract.

The extractor *proposes*; `validate_plan` and the parity gate *dispose*. This is
the same trust model as `server/reasoning.py`: a deterministic implementation is
the default and the LLM is opt-in behind a protocol, with a per-section fallback
so a failure never breaks a save.

The plan is a plain dict, validated server-side regardless of which engine
produced it:

    islands          [{blob_id, result_name}]
    values           [{value_id, result, selector, field, filters, style}]
    derived_queries  [{result_name, sql, covers: [blob_id | value_id], origin}]
    charts           [{...rendering_spec.charts shape..., replace_element_id?}]
    narrative        [{block_id, tier, goal?, inputs?, max_sentences?,
                       authored_as_of?, watch?}]
    tabs             [{id, label}] | None

Two rules give the plan its integrity, and `validate_plan` enforces both:

* **A fingerprint match is fact.** An engine may *add* an island mapping for a
  blob the matcher could not place. It may never contradict one the matcher did.
* **Nothing inferred registers silently.** Derived queries, non-editorial
  narrative tiers, and engine-added island mappings are inferences; the caller
  must round-trip them past a human before a definition is registered.

The deterministic engine proposes no inferences at all, which is what lets a
fully-fingerprinted free-form page save offline in a single call.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Protocol

from server import artifact, fingerprint
from server.env import load_dotenv

load_dotenv()

_LLM_MODEL = os.environ.get("POC_DISTILLER_MODEL", "claude-opus-4-8")
_MAX_HTML_CHARS = 24000

_TIERS = ("computed", "analytical", "editorial")

# Prose worth classifying: a block element carrying a sentence, not a label.
# Public because `server/normalizer.py` must walk the same blocks, in the same
# order, to line its block_ids up with the plan's.
PROSE_TAGS = ("p", "blockquote", "section", "article")
MIN_PROSE_CHARS = 40


@dataclass
class SessionContext:
    """Everything an extractor may look at, and nothing it may write."""

    conversation_id: str
    calls: list[dict]
    anchor_date: str
    con: object = None  # read-only DuckDB warehouse
    meta: object = None  # SQLite metadata store
    catalog: dict = field(default_factory=dict)
    prior_diff: str | None = None  # parity diff from a failed attempt


class StructureExtractor(Protocol):
    """Turn a free-form artifact plus its match report into a normalization plan."""

    def propose(
        self, html: str, report: fingerprint.MatchReport, session: SessionContext
    ) -> dict:
        ...


def empty_plan() -> dict:
    return {
        "islands": [],
        "values": [],
        "derived_queries": [],
        "charts": [],
        "narrative": [],
        "tabs": None,
    }


# ---------------------------------------------------------------------------
# Filter inference
# ---------------------------------------------------------------------------

_THOUSANDS_RE = re.compile(r"\d,\d{3}")


def infer_filters(raw_text: str) -> list[list]:
    """Read the display filters back off a rendered number.

    `+1,234` -> signed + thousands; `34.5%` -> pct(1); `-2.7pp` -> pp. Every name
    is in `artifact.FILTER_WHITELIST`, so the compiler can emit it.
    """
    text = raw_text.strip()
    filters: list[list] = []
    if text.endswith("pp"):
        filters.append(["pp", []])
    elif text.endswith("%"):
        digits = text[:-1].partition(".")[2]
        filters.append(["pct", [len(digits)]])
    elif _THOUSANDS_RE.search(text):
        filters.append(["thousands", []])
    if text.startswith("+"):
        filters.insert(0, ["signed", []])
    return filters


def _value_entry(match: fingerprint.ValueMatch) -> dict:
    return {
        "value_id": match.value_id,
        "result": match.ref.result,
        "selector": list(match.ref.selector),
        "field": match.ref.field,
        "filters": infer_filters(match.raw_text),
        "style": "sign" if match.raw_text.strip().startswith(("+", "-")) else None,
    }


def find_prose_blocks(html: str) -> list[dict]:
    """Locate prose regions worth classifying, with their source spans."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    starts = artifact.line_starts(html)
    blocks: list[dict] = []
    for tag in soup.find_all(PROSE_TAGS):
        if tag.find(PROSE_TAGS):
            continue  # a container; its children carry the prose
        text = tag.get_text(" ", strip=True)
        if len(text) < MIN_PROSE_CHARS:
            continue
        try:
            span = artifact.outer_span(html, tag, starts)
        except artifact.GrammarError:
            continue
        blocks.append(
            {
                "block_id": f"b{len(blocks)}",
                "tag": tag.name,
                "excerpt": text[:120],
                "span": span,
            }
        )
    return blocks


# ---------------------------------------------------------------------------
# The deterministic engine
# ---------------------------------------------------------------------------


class DeterministicExtractor:
    """Offline, inference-free. Passes fingerprint matches straight through.

    It proposes no derived queries, no charts, and reclassifies no prose: every
    block defaults to `editorial`, because freezing and dating text is the safe
    default and regenerating it must be opted into. A free-form artifact whose
    numbers all trace to logged results therefore normalizes and saves with zero
    network and no confirmation round-trip.
    """

    def propose(
        self, html: str, report: fingerprint.MatchReport, session: SessionContext
    ) -> dict:
        plan = empty_plan()
        plan["islands"] = [
            {"blob_id": m.blob_id, "result_name": m.result_name, "origin": "fingerprint"}
            for m in report.matches
        ]
        plan["values"] = [_value_entry(m) for m in report.value_matches]
        plan["narrative"] = [
            {
                "block_id": block["block_id"],
                "tier": "editorial",
                "authored_as_of": session.anchor_date,
                "excerpt": block["excerpt"],
                "origin": "default",
            }
            for block in find_prose_blocks(html)
        ]
        return plan


# ---------------------------------------------------------------------------
# The opt-in LLM engine
# ---------------------------------------------------------------------------

_SYSTEM = """You normalize a hand-built HTML report into a declarative contract.

You are given the report's HTML, a match report naming which data blobs and
numbers were already traced to SQL results by fingerprint, and the SQL that
produced each result.

Return ONE JSON object, no prose, with these keys:
  islands:         [{"blob_id": str, "result_name": str}]
  derived_queries: [{"result_name": str, "sql": str, "covers": [str]}]
  charts:          [{"type": "line"|"bar"|"diverging_bar", "result": str, ...}]
  values:          [{"value_id": str, "result": str, "selector": [...],
                     "field": str, "filters": [[name, [args]]]}]
  narrative:       [{"block_id": str, "tier": "computed"|"analytical"|"editorial",
                     "goal": str?, "inputs": [str]?, "max_sentences": int?}]
  tabs:            [{"id": str, "label": str}] | null

Rules you must obey:
- NEVER contradict a fingerprint match; you may only add islands for UNMATCHED blobs.
- Derived SQL must be a single SELECT that reproduces exactly the blob or value it
  claims to cover. Computation belongs in SQL, never in the page.
- Prefer "editorial" for prose that states an opinion or a thesis. Use "analytical"
  only for prose that merely describes numbers, and give it a goal and inputs.
"""


class AnthropicExtractor:
    """Propose a full plan with one model call. Any failure falls back, per section.

    Mirrors `AnthropicReasoningEngine`: no SDK, no credentials, bad JSON, or a
    network error all degrade to the deterministic plan rather than raising.
    """

    def __init__(self, model: str = _LLM_MODEL) -> None:
        self._model = model
        self._fallback = DeterministicExtractor()

    def propose(
        self, html: str, report: fingerprint.MatchReport, session: SessionContext
    ) -> dict:
        base = self._fallback.propose(html, report, session)
        try:
            import anthropic

            client = anthropic.Anthropic()
        except Exception:  # noqa: BLE001 - no SDK or no credentials
            return base

        prompt = self._prompt(html, report, session)
        for attempt in (1, 2):
            try:
                raw = self._call(client, prompt, attempt)
                proposed = json.loads(raw)
            except json.JSONDecodeError:
                if attempt == 1:
                    continue  # one retry, then give up on the model's output
                return base
            except Exception:  # noqa: BLE001 - network, rate limit, empty response
                return base
            return self._merge(base, proposed)
        return base

    def _call(self, client, prompt: str, attempt: int) -> str:
        user = prompt if attempt == 1 else prompt + "\n\nYour last reply was not valid JSON. Reply with JSON only."
        response = client.messages.create(
            model=self._model,
            max_tokens=4000,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            system=_SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        text = next((b.text for b in response.content if b.type == "text"), "").strip()
        if not text:
            raise ValueError("empty response")
        return _strip_code_fence(text)

    def _prompt(
        self, html: str, report: fingerprint.MatchReport, session: SessionContext
    ) -> str:
        matched = "\n".join(
            f"  {m.blob_id} -> {m.result_name} ({m.match_type})" for m in report.matches
        ) or "  (none)"
        unmatched = "\n".join(f"  {b.blob_id}: {b.describe()}" for b in report.unmatched) or "  (none)"
        unresolved = "\n".join(
            f"  {s.value_id}: {s.raw_text!r} (matches no result)"
            for s in report.unresolved_values
        ) or "  (none)"
        ambiguous = "\n".join(
            f"  {s.value_id}: {s.raw_text!r} could be "
            + ", ".join(sorted({f"{c.result_name}.{c.ref.field}" for c in candidates}))
            for s, candidates in report.ambiguous_values
        ) or "  (none)"
        queries = "\n".join(
            f"  {c['result_name']}: {c['sql_text']}"
            for c in session.calls
            if c.get("result_name")
        )
        blocks = "\n".join(
            f"  {b['block_id']} <{b['tag']}>: {b['excerpt']!r}" for b in find_prose_blocks(html)
        ) or "  (none)"
        parts = [
            f"REPORT HTML (truncated):\n{html[:_MAX_HTML_CHARS]}",
            f"\nFINGERPRINTED ALREADY (do not contradict):\n{matched}",
            f"\nUNMATCHED BLOBS (your work list):\n{unmatched}",
            f"\nUNRESOLVED NUMBERS (use these exact value_ids, invent none):\n{unresolved}",
            f"\nAMBIGUOUS NUMBERS (pick one result, or leave them alone):\n{ambiguous}",
            f"\nPROSE BLOCKS:\n{blocks}",
            f"\nLOGGED QUERIES:\n{queries}",
        ]
        if session.prior_diff:
            parts.append(
                f"\nA PREVIOUS ATTEMPT FAILED THE PARITY GATE:\n{session.prior_diff}\n"
                "Adjust the plan so the rendered values match the page."
            )
        return "\n".join(parts)

    def _merge(self, base: dict, proposed: dict) -> dict:
        """Take the model's sections one at a time; keep the base for any that is unusable."""
        merged = dict(base)
        fingerprinted = {i["blob_id"] for i in base["islands"]}
        for section in ("derived_queries", "charts", "tabs"):
            value = proposed.get(section)
            if isinstance(value, (list, dict)) or (section == "tabs" and value is None):
                merged[section] = value
        for entry in proposed.get("derived_queries") or []:
            if isinstance(entry, dict):
                entry.setdefault("origin", "extractor")

        extra = [
            {**i, "origin": "extractor"}
            for i in (proposed.get("islands") or [])
            if isinstance(i, dict) and i.get("blob_id") not in fingerprinted
        ]
        merged["islands"] = base["islands"] + extra

        narrative = proposed.get("narrative")
        if isinstance(narrative, list) and narrative:
            by_id = {b["block_id"]: b for b in base["narrative"]}
            out = []
            for entry in narrative:
                if not isinstance(entry, dict) or entry.get("block_id") not in by_id:
                    continue
                merged_entry = {**by_id[entry["block_id"]], **entry}
                if merged_entry.get("tier") not in _TIERS:
                    merged_entry["tier"] = "editorial"
                if merged_entry["tier"] != "editorial":
                    merged_entry["origin"] = "extractor"
                out.append(merged_entry)
            for block_id, entry in by_id.items():
                if not any(o["block_id"] == block_id for o in out):
                    out.append(entry)
            merged["narrative"] = out

        values = proposed.get("values")
        if isinstance(values, list):
            known = {v["value_id"] for v in base["values"]}
            merged["values"] = base["values"] + [
                {**v, "origin": "extractor"}
                for v in values
                if isinstance(v, dict) and v.get("value_id") not in known
            ]
        return merged


def _strip_code_fence(text: str) -> str:
    if text.startswith("```"):
        body = text.split("\n", 1)[1] if "\n" in text else ""
        return body.rsplit("```", 1)[0].strip()
    return text


def get_extractor() -> StructureExtractor:
    mode = os.environ.get("POC_DISTILLER", "deterministic").lower()
    if mode in ("anthropic", "llm", "claude"):
        return AnthropicExtractor()
    return DeterministicExtractor()


# ---------------------------------------------------------------------------
# Server-side validation -- runs for every engine, never raises
# ---------------------------------------------------------------------------


def has_inferences(plan: dict) -> bool:
    """True when anything in the plan was inferred rather than fingerprint-matched."""
    if plan.get("derived_queries"):
        return True
    if any(i.get("origin") == "extractor" for i in plan.get("islands", [])):
        return True
    if any(v.get("origin") == "extractor" for v in plan.get("values", [])):
        return True
    if any(n.get("tier", "editorial") != "editorial" for n in plan.get("narrative", [])):
        return True
    if plan.get("charts"):
        return True
    return False


def _drop(warnings: list[str], what: str, why: str) -> None:
    warnings.append(f"extraction dropped {what}: {why}")


def validate_plan(
    plan: object, report: fingerprint.MatchReport, session: SessionContext
) -> tuple[dict, list[str]]:
    """Return a plan containing only proposals the server could verify.

    Never raises. Anything unverifiable is dropped with a warning, so a garbage
    plan degrades to the fingerprint-only plan rather than failing the save.
    """
    warnings: list[str] = []
    if not isinstance(plan, dict):
        return empty_plan(), ["extraction produced no usable plan"]

    clean = empty_plan()
    known_blobs = {b.blob_id for b in report.blobs}
    truth = {m.blob_id: m.result_name for m in report.matches}
    result_names = {c["result_name"] for c in session.calls if c.get("result_name")}
    columns_by_result = {
        c["result_name"]: set((c.get("result_rows") or {}).get("columns", []))
        for c in session.calls
        if c.get("result_name")
    }

    # 1. Islands. A fingerprint match is fact; an engine may only add mappings.
    for entry in plan.get("islands") or []:
        if not isinstance(entry, dict) or "blob_id" not in entry or "result_name" not in entry:
            _drop(warnings, "an island mapping", "malformed")
            continue
        blob_id, name = entry["blob_id"], entry["result_name"]
        if blob_id not in known_blobs:
            _drop(warnings, f"island {blob_id}", "no such blob on the page")
            continue
        if blob_id in truth and truth[blob_id] != name:
            _drop(
                warnings,
                f"island {blob_id} -> {name}",
                f"contradicts the fingerprint match to {truth[blob_id]}",
            )
            continue
        clean["islands"].append(entry)

    mapped = {i["result_name"] for i in clean["islands"]}

    # 2. Derived queries: SELECT-only, dry-runnable, and they must actually
    #    reproduce what they claim to cover.
    for entry in plan.get("derived_queries") or []:
        verified = _verify_derived(entry, report, session, warnings)
        if verified:
            clean["derived_queries"].append(verified)
            mapped.add(verified["result_name"])
            columns_by_result[verified["result_name"]] = set(verified["columns"])

    # 3. Values must name a number that is actually on the page, and a selector
    #    that actually picks that number out of the named result.
    known_values = {s.value_id: s for s in report.scalars}
    rows_by_result = _rows_by_result(session.calls)
    for entry in plan.get("values") or []:
        if not isinstance(entry, dict) or not {"result", "field"} <= set(entry):
            _drop(warnings, "a value binding", "malformed")
            continue
        value_id = entry.get("value_id")
        if value_id not in known_values:
            _drop(warnings, f"value {value_id!r}", "no such number on the page")
            continue
        if entry["result"] not in result_names | mapped:
            _drop(warnings, f"value {value_id}", f"unknown result {entry['result']!r}")
            continue
        cols = columns_by_result.get(entry["result"])
        if cols and entry["field"] not in cols:
            _drop(
                warnings,
                f"value {value_id}",
                f"{entry['result']} has no field {entry['field']!r}",
            )
            continue
        if not _filters_ok(entry.get("filters") or []):
            _drop(warnings, f"value {value_id}", "unknown display filter")
            continue
        entry = {**entry, "selector": list(entry.get("selector") or ["index", 0])}
        problem = _selector_resolves(
            entry, known_values[value_id], rows_by_result.get(entry["result"])
        )
        if problem:
            _drop(warnings, f"value {value_id}", problem)
            continue
        clean["values"].append(entry)

    # 4. Charts must name a real chart type and resolvable fields.
    for spec in plan.get("charts") or []:
        if not isinstance(spec, dict):
            _drop(warnings, "a chart", "malformed")
            continue
        if spec.get("type") not in artifact.CHART_TYPES:
            _drop(warnings, f"chart {spec.get('id')}", f"unknown type {spec.get('type')!r}")
            continue
        name = spec.get("result")
        if name not in result_names | mapped:
            _drop(warnings, f"chart {spec.get('id')}", f"unknown result {name!r}")
            continue
        cols = columns_by_result.get(name) or set()
        wanted = _chart_fields(spec)
        missing = [f for f in wanted if cols and f not in cols]
        if missing:
            _drop(warnings, f"chart {spec.get('id')}", f"{name} has no field {missing[0]!r}")
            continue
        clean["charts"].append(spec)

    # 5. Narrative: tiers and watch grammar.
    for entry in plan.get("narrative") or []:
        if not isinstance(entry, dict) or "block_id" not in entry:
            _drop(warnings, "a narrative block", "malformed")
            continue
        entry = dict(entry)
        if entry.get("tier") not in _TIERS:
            entry["tier"] = "editorial"
        if entry["tier"] == "analytical" and not entry.get("goal"):
            _drop(warnings, f"analytical tier on {entry['block_id']}", "no goal given")
            entry["tier"] = "editorial"
        if entry.get("watch"):
            try:
                artifact.parse_watch(entry["watch"])
            except artifact.GrammarError as exc:
                _drop(warnings, f"watch on {entry['block_id']}", str(exc))
                entry.pop("watch")
        clean["narrative"].append(entry)

    tabs = plan.get("tabs")
    if isinstance(tabs, list) and all(
        isinstance(t, dict) and {"id", "label"} <= set(t) for t in tabs
    ):
        clean["tabs"] = tabs
    elif tabs:
        _drop(warnings, "the tab mapping", "each tab needs an id and a label")

    return clean, warnings


def _rows_by_result(calls: list[dict]) -> dict[str, tuple[list[str], list[list]]]:
    out: dict[str, tuple[list[str], list[list]]] = {}
    for call in calls:
        payload = call.get("result_rows")
        name = call.get("result_name")
        if name and payload:
            out[name] = (payload["columns"], payload["rows"])
    return out


def _selector_resolves(entry: dict, scalar, payload) -> str | None:
    """Prove the selector picks the very number the page displays.

    Shape alone is not enough. A well-formed `[esl='ORTHOPEDICS']` pointing at the
    wrong row would render the right number today and the wrong one at the next
    replay, and parity -- which compares this artifact against this data -- would
    never notice. So resolve it and compare.
    """
    selector = entry["selector"]
    if not isinstance(selector, list) or not selector:
        return f"unusable selector {selector!r}"
    kind = selector[0]

    if payload is None:
        return None  # over the row cap; nothing to resolve against
    columns, rows = payload
    if entry["field"] not in columns:
        return f"{entry['result']} has no field {entry['field']!r}"
    field_index = columns.index(entry["field"])

    if kind == "index":
        if len(selector) != 2 or not isinstance(selector[1], int):
            return f"unusable index selector {selector!r}"
        try:
            row = rows[selector[1]]
        except IndexError:
            return f"row {selector[1]} is out of range for {entry['result']}"
    elif kind == "match":
        if len(selector) != 3 or not all(isinstance(p, str) for p in selector[1:]):
            return f"unusable match selector {selector!r}"
        _, column, wanted = selector
        if column not in columns:
            return f"{entry['result']} has no column {column!r}"
        key_index = columns.index(column)
        row = next((r for r in rows if str(r[key_index]) == wanted), None)
        if row is None:
            return f"no row where {column} = {wanted!r}"
    else:
        return f"unknown selector kind {kind!r}"

    if row[field_index] != scalar.number:
        return (
            f"selector picks {row[field_index]!r}, but the page shows "
            f"{scalar.raw_text!r}"
        )
    return None


def _filters_ok(filters: list) -> bool:
    for entry in filters:
        try:
            name, args = entry[0], entry[1]
        except (TypeError, IndexError):
            return False
        if name not in artifact.FILTER_WHITELIST:
            return False
        if artifact.FILTER_WHITELIST[name] != len(args):
            return False
    return True


def _chart_fields(spec: dict) -> list[str]:
    wanted: list[str] = []
    for key in ("x", "label_field", "value_field", "display_field"):
        if spec.get(key):
            wanted.append(spec[key])
    for series in spec.get("series") or []:
        if isinstance(series, dict) and series.get("field"):
            wanted.append(series["field"])
    return wanted


def _verify_derived(
    entry: object, report: fingerprint.MatchReport, session: SessionContext, warnings: list[str]
) -> dict | None:
    """Execute a proposed query and prove it reproduces what it claims to cover."""
    from server import tools  # imported here: tools imports extractor

    if not isinstance(entry, dict) or not {"result_name", "sql"} <= set(entry):
        _drop(warnings, "a derived query", "malformed")
        return None
    name, sql = entry["result_name"], entry["sql"]

    ok, error = tools._validate_select(sql)
    if not ok:
        _drop(warnings, f"derived query {name}", error or "not a SELECT")
        return None
    dry = tools.dry_run_sql(session.conversation_id, sql)
    if not dry["valid"]:
        _drop(warnings, f"derived query {name}", f"dry run failed: {dry['error']}")
        return None

    executed = tools.execute_logged(session.conversation_id, sql, name, "save_derive")
    if "error" in executed:
        _drop(warnings, f"derived query {name}", executed["error"])
        return None

    from server.call_log import canonical_result

    canonical = canonical_result(executed["columns"], executed["rows"])
    produced = tuple(tuple(row) for row in canonical["rows"])
    columns = tuple(canonical["columns"])

    # A proposal covers blobs and/or individual numbers. Either way, coverage is
    # proven by re-deriving the data, never taken on the model's word.
    for covered in entry.get("covers") or []:
        blob = report.blob(covered)
        if blob is not None:
            if not set(blob.columns) <= set(columns):
                _drop(warnings, f"derived query {name}", f"does not produce {covered}'s columns")
                return None
            idx = [columns.index(c) for c in blob.columns]
            projected = tuple(tuple(row[i] for i in idx) for row in produced)
            if projected != blob.rows:
                _drop(warnings, f"derived query {name}", f"its rows do not reproduce {covered}")
                return None
            continue

        scalar = next((s for s in report.scalars if s.value_id == covered), None)
        if scalar is None:
            _drop(warnings, f"derived query {name}", f"covers unknown blob or value {covered!r}")
            return None
        if not any(scalar.number == value for row in produced for value in row):
            _drop(
                warnings,
                f"derived query {name}",
                f"its rows do not contain {covered}'s value {scalar.raw_text!r}",
            )
            return None

    # The plan is JSON-serialized into the confirmation token, so keep it plain.
    return {
        "result_name": name,
        "sql": sql,
        "covers": list(entry.get("covers") or []),
        "origin": "extractor",
        "columns": list(columns),
    }
