"""Parse a v2 report artifact into a structured model.

The v1 contract recovered lineage with regexes over the artifact HTML. That works
for a handful of markers but collapses once values carry selectors and filter
chains, charts carry JSON, and editorial blocks must survive replay byte-for-byte.
So the v2 contract is parsed with BeautifulSoup instead.

Two design rules hold everywhere in this module:

1. **The tree locates; it never serializes.** ``str(soup)`` is not byte-identical to
   its input (bs4 normalizes attribute quoting, re-encodes entities, and rewrites
   void elements). Every node therefore records the *offsets* of its source text, and
   the compiler splices the original string at those offsets. That is what keeps the
   untouched markup verbatim and the editorial ``html_sha256`` stable across bs4
   versions.
2. **Attributes are read through bs4, never with a regex.** ``build_value_span``
   HTML-escapes what it writes, so a ``[col='val']`` selector reaches the parser as
   ``[col=&#x27;val&#x27;]``. bs4 has already decoded it; raw source has not.

Anything unparseable lands in ``ArtifactModel.problems`` rather than raising, so the
compiler can report every defect in one pass instead of one per save attempt.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup
from bs4.element import Tag

# The filter chain a data-value may use. Deliberately closed: a filter formats a
# number that SQL already computed, so the set is small and auditable. `round` is
# Jinja's builtin (we do not register our own); the rest are registered in
# runner/render.py and mirrored in templates/runtime/charts_v1.js.
FILTER_WHITELIST: dict[str, int] = {
    # name -> number of required integer arguments
    "thousands": 0,
    "signed": 0,
    "pp": 0,
    "pct": 1,
    "round": 1,
}

CHART_TYPES = frozenset({"line", "bar", "diverging_bar"})

_WATCH_OPS = frozenset({"<", "<=", ">", ">=", "==", "!="})

# name[selector].field  — the selector is optional (legacy `result.field` = row 0).
# The selector body allows single-quoted strings with '' as the escape, so a value
# containing ']' or '.' cannot terminate it early.
_SELECTOR_BODY = r"(?:[^\]']|'(?:[^']|'')*')*"
_VALUE_REF_RE = re.compile(
    rf"^\s*([A-Za-z_]\w*)\s*(?:\[({_SELECTOR_BODY})\])?\s*\.\s*([A-Za-z_]\w*)\s*$"
)
_SEL_INDEX_RE = re.compile(r"^\s*(\d+|first|last)\s*$")
_SEL_MATCH_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*=\s*'((?:[^']|'')*)'\s*$")
_FILTER_RE = re.compile(r"^\s*([a-z_]+)\s*(?:\(\s*(-?\d+)\s*\))?\s*$")


class GrammarError(ValueError):
    """A data-value / data-inputs / data-watch string does not parse."""


@dataclass(frozen=True)
class Span:
    """Source offsets of one element in the original artifact string."""

    outer_start: int  # index of '<'
    open_end: int  # index just past the opening tag's '>'
    close_start: int  # index of the closing tag's '<' (== open_end if void)
    outer_end: int  # index just past the closing tag's '>'

    @property
    def inner(self) -> tuple[int, int]:
        return self.open_end, self.close_start


@dataclass(frozen=True)
class ValueRef:
    result: str
    selector: tuple  # ("index", n) | ("match", col, val)
    field: str
    filters: tuple[tuple[str, tuple[int, ...]], ...] = ()
    style: str | None = None
    raw: str = ""
    span: Span | None = None


@dataclass
class ArtifactModel:
    islands: dict[str, list[dict]] = field(default_factory=dict)
    island_spans: dict[str, Span] = field(default_factory=dict)
    value_refs: list[ValueRef] = field(default_factory=list)
    reasoning_steps: list[dict] = field(default_factory=list)
    legacy_reasoning: list[dict] = field(default_factory=list)
    editorial_blocks: list[dict] = field(default_factory=list)
    charts: list[dict] = field(default_factory=list)
    bound_tables: list[dict] = field(default_factory=list)
    tabs: list[dict] | None = None
    problems: list[str] = field(default_factory=list)

    def referenced_names(self) -> list[str]:
        """Every result name the artifact refers to, in first-seen order."""
        names: list[str] = []

        def add(name: str) -> None:
            if name and name not in names:
                names.append(name)

        for name in self.islands:
            add(name)
        for ref in self.value_refs:
            add(ref.result)
        for chart in self.charts:
            add(chart.get("result", ""))
        for table in self.bound_tables:
            add(table["result"])
        for step in self.reasoning_steps:
            for inp in step["inputs"]:
                add(inp["result_name"])
        for block in self.editorial_blocks:
            watch = block.get("watch")
            if watch:
                add(watch["ref"].result)
        return names


# --------------------------------------------------------------------------
# Source-offset machinery
# --------------------------------------------------------------------------

# Elements whose content is raw text: a '<' inside them never starts a tag.
_RAW_TEXT = frozenset({"script", "style"})
# Elements that never have a closing tag.
_VOID = frozenset(
    "area base br col embed hr img input link meta param source track wbr".split()
)


def line_starts(html: str) -> list[int]:
    """Index of the first character of each 1-based line."""
    starts = [0]
    idx = html.find("\n")
    while idx != -1:
        starts.append(idx + 1)
        idx = html.find("\n", idx + 1)
    return starts


def _abs_offset(tag: Tag, starts: list[int]) -> int:
    if tag.sourceline is None or tag.sourcepos is None:
        raise GrammarError(
            f"<{tag.name}> has no source position; bs4's html.parser must be used "
            "with source-position tracking (bs4 >= 4.8.1)"
        )
    return starts[tag.sourceline - 1] + tag.sourcepos


def _tag_end(html: str, start: int) -> int:
    """Index just past the '>' that closes the tag beginning at `start`.

    Quote-aware: an attribute value may legitimately contain '>' (a data-chart
    JSON blob does), so a naive scan to the next '>' truncates the tag.
    """
    i = start + 1
    quote = ""
    while i < len(html):
        ch = html[i]
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == ">":
            return i + 1
        i += 1
    raise GrammarError(f"unterminated tag at offset {start}")


def outer_span(html: str, tag: Tag, starts: list[int]) -> Span:
    """Locate `tag`'s full outer HTML in the original source."""
    start = _abs_offset(tag, starts)
    open_end = _tag_end(html, start)
    name = tag.name.lower()

    if name in _VOID or html[open_end - 2 : open_end] == "/>":
        return Span(start, open_end, open_end, open_end)

    if name in _RAW_TEXT:
        close = html.find(f"</{name}", open_end)
        if close == -1:
            raise GrammarError(f"unterminated <{name}> at offset {start}")
        return Span(start, open_end, close, _tag_end(html, close))

    # Walk forward, counting only same-name tags, so nested markup of other names
    # (and any '>' hiding inside their attributes) cannot end us early.
    depth = 1
    i = open_end
    while i < len(html):
        lt = html.find("<", i)
        if lt == -1:
            break
        closing = html[lt + 1 : lt + 2] == "/"
        name_start = lt + 2 if closing else lt + 1
        match = re.match(r"[a-zA-Z][\w:-]*", html[name_start:])
        if not match:
            i = lt + 1
            continue
        found = match.group(0).lower()
        end = _tag_end(html, lt)
        if found == name:
            if closing:
                depth -= 1
                if depth == 0:
                    return Span(start, open_end, lt, end)
            elif html[end - 2 : end] != "/>":
                depth += 1
        elif found in _RAW_TEXT and not closing:
            skip = html.find(f"</{found}", end)
            end = _tag_end(html, skip) if skip != -1 else end
        i = end
    raise GrammarError(f"unterminated <{name}> at offset {start}")


# --------------------------------------------------------------------------
# Value grammar
# --------------------------------------------------------------------------


def _unescape_sql_quotes(text: str) -> str:
    return text.replace("''", "'")


def split_outside_brackets(text: str, sep: str) -> list[str]:
    """Split on `sep` that is not inside [...] or a single-quoted string."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote = False
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "'":
                if text[i + 1 : i + 2] == "'":
                    buf.append("''")
                    i += 2
                    continue
                quote = False
        elif ch == "'":
            quote = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def parse_selector(body: str | None) -> tuple:
    """`None` -> row 0 (legacy). `[3]` / `[first]` / `[last]` / `[col='val']`."""
    if body is None:
        return ("index", 0)
    if (match := _SEL_INDEX_RE.match(body)) is not None:
        token = match.group(1)
        if token == "first":
            return ("index", 0)
        if token == "last":
            return ("index", -1)
        return ("index", int(token))
    if (match := _SEL_MATCH_RE.match(body)) is not None:
        return ("match", match.group(1), _unescape_sql_quotes(match.group(2)))
    raise GrammarError(f"unrecognized selector '[{body}]'")


def parse_filters(chain: str) -> tuple[tuple[str, tuple[int, ...]], ...]:
    filters: list[tuple[str, tuple[int, ...]]] = []
    for token in split_outside_brackets(chain, "|"):
        match = _FILTER_RE.match(token)
        if not match:
            raise GrammarError(f"unparseable filter '{token}'")
        name, arg = match.group(1), match.group(2)
        if name not in FILTER_WHITELIST:
            raise GrammarError(f"filter '{name}' is not whitelisted")
        arity = FILTER_WHITELIST[name]
        if arity and arg is None:
            raise GrammarError(f"filter '{name}' requires an argument")
        if not arity and arg is not None:
            raise GrammarError(f"filter '{name}' takes no argument")
        filters.append((name, (int(arg),) if arg is not None else ()))
    return tuple(filters)


def parse_value_ref(raw: str) -> tuple[str, tuple, str, tuple]:
    """`result[sel].field | f1 | f2` -> (result, selector, field, filters)."""
    head, sep, chain = raw.partition("|")
    match = _VALUE_REF_RE.match(head)
    if not match:
        raise GrammarError(f"unparseable data-value '{raw.strip()}'")
    result, sel_body, field_name = match.groups()
    selector = parse_selector(sel_body)
    filters = parse_filters(chain) if sep else ()
    return result, selector, field_name, filters


def parse_input_ref(raw: str) -> dict:
    """`name` or `name[col='val']` -> {result_name, filter}."""
    text = raw.strip()
    match = re.match(rf"^([A-Za-z_]\w*)\s*(?:\[({_SELECTOR_BODY})\])?$", text)
    if not match:
        raise GrammarError(f"unparseable data-inputs entry '{text}'")
    name, body = match.groups()
    if body is None:
        return {"result_name": name, "filter": None}
    selector = parse_selector(body)
    if selector[0] != "match":
        raise GrammarError(f"data-inputs entry '{text}' must use [col='val']")
    return {"result_name": name, "filter": {"col": selector[1], "val": selector[2]}}


def parse_watch(raw: str) -> dict:
    """`valueref OP number` -> {raw, ref, op, value}."""
    match = re.match(r"^\s*(.+?)\s*(<=|>=|==|!=|<|>)\s*(-?\d+(?:\.\d+)?)\s*$", raw)
    if not match:
        raise GrammarError(f"unparseable data-watch '{raw.strip()}'")
    ref_text, op, number = match.groups()
    if op not in _WATCH_OPS:
        raise GrammarError(f"unsupported watch operator '{op}'")
    result, selector, field_name, filters = parse_value_ref(ref_text)
    if filters:
        raise GrammarError("data-watch reference must not carry filters")
    ref = ValueRef(result, selector, field_name, raw=ref_text.strip())
    return {"raw": raw.strip(), "ref": ref, "op": op, "value": float(number)}


# --------------------------------------------------------------------------
# Section parsers
# --------------------------------------------------------------------------


def _normalize_island(payload: object) -> list[dict]:
    if isinstance(payload, list):
        if not all(isinstance(row, dict) for row in payload):
            raise GrammarError("island array must contain row objects")
        return payload
    if isinstance(payload, dict) and "rows" in payload:
        columns = payload.get("columns")
        rows = payload["rows"]
        if not isinstance(rows, list):
            raise GrammarError("island 'rows' must be a list")
        if rows and isinstance(rows[0], list):
            if not columns:
                raise GrammarError("island with list rows needs 'columns'")
            return [dict(zip(columns, row)) for row in rows]
        return rows
    raise GrammarError("island must be a row array or {columns, rows}")


def _parse_islands(soup: BeautifulSoup, html: str, starts: list[int], model) -> None:
    for tag in soup.select('script[type="application/json"][data-result]'):
        name = tag["data-result"]
        try:
            span = outer_span(html, tag, starts)
            payload = json.loads(html[span.open_end : span.close_start])
            model.islands[name] = _normalize_island(payload)
            model.island_spans[name] = span
        except (json.JSONDecodeError, GrammarError) as exc:
            model.problems.append(f"island '{name}': {exc}")


def _parse_values(soup: BeautifulSoup, html: str, starts: list[int], model) -> None:
    for tag in soup.select("[data-value]"):
        raw = tag["data-value"]
        try:
            result, selector, field_name, filters = parse_value_ref(raw)
            model.value_refs.append(
                ValueRef(
                    result=result,
                    selector=selector,
                    field=field_name,
                    filters=filters,
                    style=tag.get("data-style"),
                    raw=raw,
                    span=outer_span(html, tag, starts),
                )
            )
        except GrammarError as exc:
            model.problems.append(f"data-value: {exc}")


def _parse_reasoning(soup: BeautifulSoup, html: str, starts: list[int], model) -> None:
    for tag in soup.select("[data-reasoning]"):
        step_id = tag["data-reasoning"]
        if tag.has_attr("data-over"):
            model.legacy_reasoning.append({"step_id": step_id, "tag": tag})
            continue
        if not tag.has_attr("data-goal"):
            model.problems.append(
                f"reasoning '{step_id}' has neither data-goal (v2) nor data-over (v1)"
            )
            continue
        try:
            raw_inputs = tag.get("data-inputs", "")
            inputs = [
                parse_input_ref(part)
                for part in split_outside_brackets(raw_inputs, ",")
            ]
            if not inputs:
                raise GrammarError("data-inputs is empty")
            max_sentences = int(tag.get("data-max-sentences", 3))
            model.reasoning_steps.append(
                {
                    "step_id": step_id,
                    "goal": tag["data-goal"],
                    "inputs": inputs,
                    "max_sentences": max_sentences,
                    "style": tag.get("data-style"),
                    "span": outer_span(html, tag, starts),
                }
            )
        except (GrammarError, ValueError) as exc:
            model.problems.append(f"reasoning '{step_id}': {exc}")


def _parse_editorial(soup: BeautifulSoup, html: str, starts: list[int], model) -> None:
    for tag in soup.select("[data-editorial]"):
        block_id = tag["data-editorial"]
        try:
            span = outer_span(html, tag, starts)
            source = html[span.outer_start : span.outer_end]
            watch = parse_watch(tag["data-watch"]) if tag.has_attr("data-watch") else None
            model.editorial_blocks.append(
                {
                    "block_id": block_id,
                    "html": source,
                    "html_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                    "authored_as_of": tag.get("data-authored-as-of"),
                    "watch": watch,
                    "span": span,
                }
            )
        except GrammarError as exc:
            model.problems.append(f"editorial '{block_id}': {exc}")


def _parse_charts(soup: BeautifulSoup, model) -> None:
    for tag in soup.select("[data-chart]"):
        try:
            spec = json.loads(tag["data-chart"])
        except json.JSONDecodeError as exc:
            model.problems.append(f"chart on <{tag.name}>: invalid JSON ({exc})")
            continue
        if not isinstance(spec, dict):
            model.problems.append(f"chart on <{tag.name}>: declaration must be an object")
            continue
        kind = spec.get("type")
        if kind not in CHART_TYPES:
            model.problems.append(f"chart on <{tag.name}>: unknown type {kind!r}")
            continue
        if not spec.get("result"):
            model.problems.append(f"chart on <{tag.name}>: missing 'result'")
            continue
        spec.setdefault("id", tag.get("id"))
        model.charts.append(spec)


def _parse_column_spec(spec: str) -> dict:
    """`field:Header|thousands|style:sign` -> {field, header, filters, style}."""
    parts = [p.strip() for p in spec.split("|")]
    head = parts[0]
    if ":" not in head:
        raise GrammarError(f"column '{head}' must be 'field:Header'")
    field_name, header = (p.strip() for p in head.split(":", 1))
    filters: list[tuple[str, tuple[int, ...]]] = []
    style = None
    for token in parts[1:]:
        if token.startswith("style:"):
            style = token.split(":", 1)[1].strip()
            continue
        filters.extend(parse_filters(token))
    return {
        "field": field_name,
        "header": header,
        "filters": [[name, list(args)] for name, args in filters],
        "style": style,
    }


def _parse_bound_tables(soup: BeautifulSoup, model) -> None:
    for tag in soup.select("table[data-result][data-columns]"):
        name = tag["data-result"]
        body = tag.find("tbody")
        if body is not None and body.find("tr") is not None:
            model.problems.append(
                f"bound table '{name}' must have an empty <tbody>; the runtime fills it"
            )
            continue
        try:
            columns = [
                _parse_column_spec(part)
                for part in split_outside_brackets(tag["data-columns"], ",")
            ]
        except GrammarError as exc:
            model.problems.append(f"bound table '{name}': {exc}")
            continue
        model.bound_tables.append(
            {"result": name, "columns": columns, "id": tag.get("id")}
        )


def _parse_tabs(soup: BeautifulSoup, model) -> None:
    tag = soup.select_one("nav[data-tabs]")
    if tag is None:
        return
    try:
        tabs = json.loads(tag["data-tabs"])
    except json.JSONDecodeError as exc:
        model.problems.append(f"tabs: invalid JSON ({exc})")
        return
    if not isinstance(tabs, list) or not all(
        isinstance(t, dict) and "id" in t and "label" in t for t in tabs
    ):
        model.problems.append("tabs: expected a list of {id, label} objects")
        return
    model.tabs = tabs


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------

# A legacy artifact matches none of these. Keep it that way: the v1 data-value
# regex happily reads `race[last].gap | thousands` as result 'race[last]', field
# 'gap | thousands', so a v2 artifact that leaks into the legacy path is corrupted
# silently rather than loudly.
_V2_MARKERS = (
    re.compile(r'<script[^>]*type="application/json"[^>]*data-result=', re.I),
    re.compile(r"\bdata-editorial\b", re.I),
    re.compile(r"\bdata-chart\b", re.I),
    re.compile(r"\bdata-columns\b", re.I),
    re.compile(r"\bdata-goal\b", re.I),
    re.compile(r"\bdata-tabs\b", re.I),
    re.compile(r'data-value="[^"]*(?:\[|\||&#x27;)', re.I),
)


def is_v2(html: str) -> bool:
    """True when the artifact uses any v2 marker."""
    return any(marker.search(html) for marker in _V2_MARKERS)


def parse(html: str) -> ArtifactModel:
    """Parse a v2 artifact body fragment. Defects accumulate in `problems`."""
    soup = BeautifulSoup(html, "html.parser")
    starts = line_starts(html)
    model = ArtifactModel()
    _parse_islands(soup, html, starts, model)
    _parse_values(soup, html, starts, model)
    _parse_reasoning(soup, html, starts, model)
    _parse_editorial(soup, html, starts, model)
    _parse_charts(soup, model)
    _parse_bound_tables(soup, model)
    _parse_tabs(soup, model)
    return model
