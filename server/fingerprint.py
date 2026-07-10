"""Deterministic matching of a free-form page's numbers against logged results.

Pure code: no LLM, no database, no JavaScript engine. Given the artifact HTML and
the conversation's logged calls (which carry canonical rows since WS11-A), find
every blob and scalar on the page that *provably* came from a query result.

The output is a `MatchReport`. What it matched is fact -- a fingerprint equality,
not a guess -- and needs no human confirmation. What it could not match is the
extractor's work list, and anything the extractor infers from that list must be
confirmed before a definition is registered.

Three kinds of blob are recovered, each with its source `Span` so the normalizer
can splice the original bytes:

* **JS constants** -- ``const RACE = [ ... ];`` read by a tolerant literal reader
  (below). Also grouped dicts: ``const ESL_QTR = {ORTHOPEDICS: [...], ...}``. Only
  declarations that open an array or object are considered: a local ``var w = 720``
  is code, not a table, and reporting it as untraceable data would be noise.
* **HTML tables** with populated bodies.
* **Scalar candidates** -- a number in a short element, the shape a KPI takes.

Matching runs in strictness order (row-set, then projection, then grouped, then
scalar), and a blob is claimed by the first rule that fits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from server import artifact
from server.call_log import canonical_scalar, fingerprint_canonical

# Elements whose text is short enough to plausibly *be* a number, rather than
# prose that happens to contain one.
_SCALAR_TAGS = ("strong", "b", "span", "em", "td", "th", "div", "p", "h1", "h2", "h3")
_MAX_SCALAR_CHARS = 32

# Mirrors linter._BOUND_ATTRS: a number already bound to a result is not a
# candidate, it is already structure.
_BOUND_ATTRS = ("data-value", "data-editorial", "data-reasoning", "data-chart", "data-result")

# A data blob is a declaration whose right-hand side *opens* an array or object.
# `var w = 720` and `var svg = document.getElementById(...)` are code, not tables,
# and must not be mistaken for data the page failed to trace.
_CONST_RE = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?=[\[{])", re.MULTILINE
)


class NotALiteral(ValueError):
    """The JavaScript source is not a pure data literal (it computes something)."""


# ---------------------------------------------------------------------------
# The tolerant JS literal reader
# ---------------------------------------------------------------------------
#
# Rejection is by construction, in three layers, so a computed value can never be
# mistaken for data:
#
#   1. The tokenizer accepts only structural punctuation, strings, numbers, and
#      barewords. Any other character -- `+ - * / ( ) < > = & | ! ? %` outside a
#      number or string -- raises. That alone kills arithmetic, calls, ternaries.
#   2. In value position only object/array/string/number/true/false/null are
#      accepted. Any other bareword raises, which kills identifier references
#      like `TOTAL` or `d.uhs`.
#   3. After a completed value the next token must be `,`, `]`, `}`, or EOF.
#
# We accept what hand-written JS data actually looks like: unquoted keys, single
# quotes, trailing commas.

_NUMBER_RE = re.compile(r"-?(?:0|[1-9]\d*|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?")
_BAREWORD_RE = re.compile(r"[A-Za-z_$][\w$]*")
_PUNCT = set("{}[],:")
_MAX_DEPTH = 32
_LITERAL_WORDS = {"true": True, "false": False, "null": None}


class _Reader:
    def __init__(self, text: str) -> None:
        self.text = text
        self.i = 0

    def error(self, message: str) -> NotALiteral:
        return NotALiteral(f"{message} at offset {self.i}")

    def skip_space(self) -> None:
        while self.i < len(self.text) and self.text[self.i] in " \t\r\n":
            self.i += 1

    def peek(self) -> str | None:
        self.skip_space()
        return self.text[self.i] if self.i < len(self.text) else None

    def read_string(self) -> str:
        quote = self.text[self.i]
        self.i += 1
        out: list[str] = []
        while self.i < len(self.text):
            ch = self.text[self.i]
            if ch == "\\":
                if self.i + 1 >= len(self.text):
                    raise self.error("unterminated escape")
                nxt = self.text[self.i + 1]
                out.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
                self.i += 2
                continue
            if ch == quote:
                self.i += 1
                return "".join(out)
            out.append(ch)
            self.i += 1
        raise self.error("unterminated string")

    def read_value(self, depth: int = 0) -> object:
        if depth > _MAX_DEPTH:
            raise self.error("nesting too deep")
        ch = self.peek()
        if ch is None:
            raise self.error("unexpected end of input")
        if ch == "{":
            return self.read_object(depth)
        if ch == "[":
            return self.read_array(depth)
        if ch in "\"'":
            return self.read_string()

        match = _NUMBER_RE.match(self.text, self.i)
        if match and (ch.isdigit() or ch == "-"):
            self.i = match.end()
            raw = match.group(0)
            return float(raw) if any(c in raw for c in ".eE") else int(raw)

        word = _BAREWORD_RE.match(self.text, self.i)
        if word:
            token = word.group(0)
            if token in _LITERAL_WORDS:
                self.i = word.end()
                return _LITERAL_WORDS[token]
            # An identifier in value position means the value is computed or
            # borrowed from elsewhere -- not data we can fingerprint.
            raise self.error(f"identifier {token!r} in value position")

        raise self.error(f"unexpected character {ch!r}")

    def read_array(self, depth: int) -> list:
        self.i += 1  # consume '['
        out: list = []
        while True:
            if self.peek() == "]":
                self.i += 1
                return out
            out.append(self.read_value(depth + 1))
            nxt = self.peek()
            if nxt == ",":
                self.i += 1
                continue
            if nxt == "]":
                self.i += 1
                return out
            raise self.error(f"expected ',' or ']', found {nxt!r}")

    def read_object(self, depth: int) -> dict:
        self.i += 1  # consume '{'
        out: dict = {}
        while True:
            ch = self.peek()
            if ch == "}":
                self.i += 1
                return out
            if ch in "\"'":
                key = self.read_string()
            else:
                word = _BAREWORD_RE.match(self.text, self.i)
                if not word:
                    raise self.error("expected an object key")
                key = word.group(0)
                self.i = word.end()
            if self.peek() != ":":
                raise self.error(f"expected ':' after key {key!r}")
            self.i += 1
            out[key] = self.read_value(depth + 1)
            nxt = self.peek()
            if nxt == ",":
                self.i += 1
                continue
            if nxt == "}":
                self.i += 1
                return out
            raise self.error(f"expected ',' or '}}', found {nxt!r}")


def read_js_literal(text: str) -> object:
    """Parse a JS data literal. Raise `NotALiteral` on anything that computes."""
    reader = _Reader(text)
    value = reader.read_value()
    reader.skip_space()
    if reader.i != len(reader.text):
        raise reader.error("trailing content after the literal")
    return value


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Blob:
    """A tabular chunk of data found on the page, with where it came from."""

    blob_id: str
    kind: str  # "js_const" | "js_grouped_dict" | "html_table"
    name: str | None
    columns: tuple[str, ...]
    rows: tuple[tuple, ...]  # canonical scalars, in page order
    span: artifact.Span
    value_span: tuple[int, int] | None = None  # the RHS of a `const NAME = ...;`
    group_keys: tuple[str, ...] = ()  # for js_grouped_dict, the dict's keys in order

    def describe(self) -> str:
        what = f"const {self.name}" if self.name else f"<table> ({self.kind})"
        return f"{what} ({len(self.rows)} rows x {len(self.columns)} columns)"


@dataclass(frozen=True)
class ScalarCandidate:
    value_id: str
    number: object
    raw_text: str
    span: artifact.Span
    tag: str


@dataclass(frozen=True)
class BlobMatch:
    blob_id: str
    result_name: str
    match_type: str  # "rowset" | "projection" | "grouped"
    columns: tuple[str, ...] = ()
    grouped_by: str | None = None


@dataclass(frozen=True)
class ValueMatch:
    value_id: str
    result_name: str
    ref: artifact.ValueRef
    span: artifact.Span
    raw_text: str


@dataclass
class MatchReport:
    blobs: list[Blob] = field(default_factory=list)
    scalars: list[ScalarCandidate] = field(default_factory=list)
    matches: list[BlobMatch] = field(default_factory=list)
    value_matches: list[ValueMatch] = field(default_factory=list)
    unmatched: list[Blob] = field(default_factory=list)
    unresolved_values: list[ScalarCandidate] = field(default_factory=list)
    ambiguous_values: list[tuple[ScalarCandidate, list[ValueMatch]]] = field(default_factory=list)
    unparseable: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def blob(self, blob_id: str) -> Blob | None:
        return next((b for b in self.blobs if b.blob_id == blob_id), None)

    def matched_blob_ids(self) -> set[str]:
        return {m.blob_id for m in self.matches}


# ---------------------------------------------------------------------------
# Blob extraction
# ---------------------------------------------------------------------------


def _tabular(value: object) -> tuple[tuple[str, ...], tuple[tuple, ...]] | None:
    """A list of uniform row objects -> (columns, canonical rows)."""
    if not isinstance(value, list) or not value:
        return None
    if not all(isinstance(row, dict) for row in value):
        return None
    columns = tuple(value[0].keys())
    if any(tuple(row.keys()) != columns for row in value):
        return None
    rows = tuple(tuple(canonical_scalar(row[c]) for c in columns) for row in value)
    return columns, rows


def _grouped(value: object) -> tuple[tuple[str, ...], dict[str, tuple[tuple, ...]]] | None:
    """A dict of key -> list of uniform row objects."""
    if not isinstance(value, dict) or not value:
        return None
    groups: dict[str, tuple[tuple, ...]] = {}
    columns: tuple[str, ...] | None = None
    for key, rows in value.items():
        table = _tabular(rows)
        if table is None:
            return None
        cols, canonical_rows = table
        if columns is None:
            columns = cols
        elif cols != columns:
            return None
        groups[str(key)] = canonical_rows
    return (columns or ()), groups


def _scan_rhs(body: str, start: int) -> int:
    """Index just past the `;` (or the balanced end) of a value starting at `start`."""
    depth = 0
    i = start
    while i < len(body):
        ch = body[i]
        if ch in "\"'":
            quote = ch
            i += 1
            while i < len(body):
                if body[i] == "\\":
                    i += 2
                    continue
                if body[i] == quote:
                    break
                i += 1
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                # Consume an optional trailing semicolon.
                j = i + 1
                while j < len(body) and body[j] in " \t\r\n":
                    j += 1
                return j + 1 if j < len(body) and body[j] == ";" else i + 1
        elif ch == ";" and depth == 0:
            return i + 1
        i += 1
    return len(body)


def extract_js_consts(html: str, soup: BeautifulSoup, starts: list[int], report: MatchReport) -> list[Blob]:
    """Find `const NAME = <literal>;` statements inside <script> bodies."""
    blobs: list[Blob] = []
    for tag in soup.find_all("script"):
        if tag.get("type") == "application/json":
            continue  # already a data island
        try:
            span = artifact.outer_span(html, tag, starts)
        except artifact.GrammarError:
            continue
        body = html[span.open_end : span.close_start]
        for match in _CONST_RE.finditer(body):
            name = match.group(1)
            value_start = match.end()
            value_end = _scan_rhs(body, value_start)
            source = body[value_start:value_end].rstrip().rstrip(";")
            try:
                value = read_js_literal(source)
            except NotALiteral:
                report.unparseable.append(name)
                continue

            abs_span = (span.open_end + match.start(), span.open_end + value_end)
            blob_id = f"const:{name}"
            table = _tabular(value)
            if table is not None:
                columns, rows = table
                blobs.append(
                    Blob(blob_id, "js_const", name, columns, rows, span, abs_span)
                )
                continue
            group = _grouped(value)
            if group is not None:
                columns, groups = group
                flat = tuple(row for rows in groups.values() for row in rows)
                blobs.append(
                    Blob(
                        blob_id,
                        "js_grouped_dict",
                        name,
                        columns,
                        flat,
                        span,
                        abs_span,
                        tuple(groups.keys()),
                    )
                )
                continue
            # A literal that is neither tabular nor grouped (a scalar, a config
            # object). Not evidence of a result; not an error either.
    return blobs


def extract_html_tables(html: str, soup: BeautifulSoup, starts: list[int]) -> list[Blob]:
    blobs: list[Blob] = []
    for index, table in enumerate(soup.find_all("table")):
        if table.has_attr("data-result"):
            continue  # already bound
        body = table.find("tbody") or table
        trs = [tr for tr in body.find_all("tr") if tr.find("td")]
        if not trs:
            continue
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        width = max(len(tr.find_all("td")) for tr in trs)
        columns = tuple(headers[:width]) if len(headers) >= width else tuple(
            f"col{i}" for i in range(width)
        )
        rows = tuple(
            tuple(
                canonical_scalar(td.get_text(strip=True), from_text=True)
                for td in tr.find_all("td")
            )
            for tr in trs
        )
        if any(len(row) != width for row in rows):
            continue
        try:
            span = artifact.outer_span(html, table, starts)
        except artifact.GrammarError:
            continue
        blobs.append(Blob(f"table:{index}", "html_table", None, columns, rows, span))
    return blobs


def _inside_bound_element(node) -> bool:
    for ancestor in node.parents:
        attrs = getattr(ancestor, "attrs", None)
        if attrs and any(attr in attrs for attr in _BOUND_ATTRS):
            return True
    return False


def extract_scalars(html: str, soup: BeautifulSoup, starts: list[int]) -> list[ScalarCandidate]:
    """Numbers sitting in short leaf elements -- the shape a KPI value takes."""
    scalars: list[ScalarCandidate] = []
    for tag in soup.find_all(_SCALAR_TAGS):
        if tag.find(_SCALAR_TAGS):
            continue  # not a leaf; its child will be considered instead
        if _inside_bound_element(tag) or any(a in tag.attrs for a in _BOUND_ATTRS):
            continue
        if tag.find_parent("table") is not None:
            continue  # table cells are claimed by extract_html_tables
        text = tag.get_text(strip=True)
        if not text or len(text) > _MAX_SCALAR_CHARS:
            continue
        number = canonical_scalar(text, from_text=True)
        if not isinstance(number, float):
            continue
        try:
            span = artifact.outer_span(html, tag, starts)
        except artifact.GrammarError:
            continue
        scalars.append(
            ScalarCandidate(f"value:{len(scalars)}", number, text, span, tag.name)
        )
    return scalars


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _result_rows(call: dict) -> tuple[tuple[str, ...], tuple[tuple, ...]] | None:
    payload = call.get("result_rows")
    if not payload:
        return None
    return tuple(payload["columns"]), tuple(tuple(row) for row in payload["rows"])


def _project(columns: tuple[str, ...], rows: tuple[tuple, ...], wanted: tuple[str, ...]):
    idx = [columns.index(c) for c in wanted]
    return tuple(tuple(row[i] for i in idx) for row in rows)


def _first_non_numeric_column(columns: tuple[str, ...], row: tuple) -> str | None:
    for col, value in zip(columns, row):
        if isinstance(value, str):
            return col
    return None


def _match_blob(blob: Blob, calls: list[dict]) -> BlobMatch | None:
    for call in calls:
        name = call.get("result_name")
        if not name:
            continue
        payload = _result_rows(call)

        if payload is None:
            # Over the size cap: fingerprint equality is all we have, and it only
            # decides an exact row-set match.
            if blob.kind == "js_const" and call.get("result_fingerprint"):
                canonical = {
                    "columns": list(blob.columns),
                    "rows": [list(r) for r in blob.rows],
                }
                if fingerprint_canonical(canonical) == call["result_fingerprint"]:
                    return BlobMatch(blob.blob_id, name, "rowset", blob.columns)
            continue

        columns, rows = payload

        if blob.kind == "js_grouped_dict":
            grouped = _match_grouped(blob, name, columns, rows)
            if grouped:
                return grouped
            continue

        if blob.columns == columns and blob.rows == rows:
            return BlobMatch(blob.blob_id, name, "rowset", blob.columns)

        if set(blob.columns) <= set(columns) and len(blob.rows) == len(rows):
            if blob.rows == _project(columns, rows, blob.columns):
                return BlobMatch(blob.blob_id, name, "projection", blob.columns)
    return None


def _match_grouped(blob: Blob, name: str, columns: tuple[str, ...], rows: tuple[tuple, ...]):
    """A dict keyed by a column value, e.g. {ORTHOPEDICS: [...]} vs long-form rows."""
    if not set(blob.columns) <= set(columns):
        return None
    # Try each column the dict's rows do *not* carry as the grouping key: that is
    # the column whose values became the dict's keys.
    for key_col in columns:
        if key_col in blob.columns:
            continue  # the key is not carried inside the group's rows
        groups: dict[str, list[tuple]] = {}
        key_index = columns.index(key_col)
        for row in rows:
            groups.setdefault(str(row[key_index]), []).append(row)
        if set(groups) != set(blob.group_keys):
            continue
        projected: list[tuple] = []
        for key in blob.group_keys:
            projected.extend(_project(columns, tuple(groups[key]), blob.columns))
        if tuple(projected) == blob.rows:
            return BlobMatch(blob.blob_id, name, "grouped", blob.columns, key_col)
    return None


def _match_scalar(scalar: ScalarCandidate, calls: list[dict]) -> list[ValueMatch]:
    """Every (result, row, field) whose value equals this number."""
    found: list[ValueMatch] = []
    for call in calls:
        name = call.get("result_name")
        payload = _result_rows(call)
        if not name or payload is None:
            continue
        columns, rows = payload
        for row_index, row in enumerate(rows):
            for col_index, value in enumerate(row):
                if value != scalar.number:
                    continue
                field_name = columns[col_index]
                if len(rows) == 1:
                    selector: tuple = ("index", 0)
                else:
                    key_col = _first_non_numeric_column(columns, row)
                    if key_col is None:
                        selector = ("index", row_index)
                    else:
                        key_val = row[columns.index(key_col)]
                        selector = ("match", key_col, str(key_val))
                ref = artifact.ValueRef(
                    result=name,
                    selector=selector,
                    field=field_name,
                    raw=f"{name}.{field_name}",
                    span=scalar.span,
                )
                found.append(
                    ValueMatch(scalar.value_id, name, ref, scalar.span, scalar.raw_text)
                )
    return found


def match(html: str, calls: list[dict]) -> MatchReport:
    """Fingerprint every blob and scalar on the page against the logged results."""
    report = MatchReport()
    soup = BeautifulSoup(html, "html.parser")
    starts = artifact.line_starts(html)

    report.blobs = extract_js_consts(html, soup, starts, report) + extract_html_tables(
        html, soup, starts
    )
    report.scalars = extract_scalars(html, soup, starts)

    for call in calls:
        if call.get("result_fingerprint") and call.get("result_rows") is None:
            report.notes.append(
                f"result '{call.get('result_name')}' exceeded the row cap; only an "
                "exact row-set match is possible against it"
            )

    for blob in report.blobs:
        found = _match_blob(blob, calls)
        if found:
            report.matches.append(found)
        else:
            report.unmatched.append(blob)

    claimed_by_blob = _spans_of_matched_blobs(report)
    for scalar in report.scalars:
        if _within_any(scalar.span, claimed_by_blob):
            continue
        candidates = _match_scalar(scalar, calls)
        if not candidates:
            report.unresolved_values.append(scalar)
        elif len({(c.result_name, c.ref.field, c.ref.selector) for c in candidates}) == 1:
            report.value_matches.append(candidates[0])
        else:
            report.ambiguous_values.append((scalar, candidates))
    return report


def _spans_of_matched_blobs(report: MatchReport) -> list[artifact.Span]:
    matched = report.matched_blob_ids()
    return [b.span for b in report.blobs if b.blob_id in matched]


def _within_any(span: artifact.Span, outers: list[artifact.Span]) -> bool:
    return any(
        outer.outer_start <= span.outer_start and span.outer_end <= outer.outer_end
        for outer in outers
    )
