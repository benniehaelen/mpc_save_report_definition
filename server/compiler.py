"""Distillation: turn a session record into a report definition.

This POC uses a deterministic heuristic, which is enough to prove the
architecture. The real system uses an LLM here; the Distiller protocol below
makes that swap trivial (one distill method).

The distilled definition has the four parts from the design:
  - parameterized_sql: the named query set, with date literals tokenized
  - metric_bindings:   result fields bound to governed metrics and ValueSets
  - reasoning_steps:   steps the runner replays over fresh results for narrative
  - rendering_spec:    the template plus the output formats and title

Heuristic rules:
  1. Parse the final artifact. Every <table data-result="...">, every element
     with a data-value attribute, and every element with a data-reasoning
     attribute names a result.
  2. Match those names against result_name values in the tool-call log. Only
     matched queries enter the definition, so superseded and dead-end queries
     drop out because nothing in the artifact references them.
  3. Pass each surviving query through temporal.reparameterize.
  4. Bind fields (table headers and data-value fields) to catalog metrics and
     ValueSets.
  5. Convert the artifact into a Jinja2 template: table bodies become loops,
     data-value numbers become expressions, and data-reasoning elements become
     narrative expressions. Anything that cannot be templated is reported as an
     unreplayable section rather than silently kept.

There are two artifact contracts, and `distill` dispatches between them on the
markers present (see `server/artifact.is_v2`):

  * **v1** -- populated `<table data-result>` bodies, `data-value="result.field"`,
    and `data-reasoning` + `data-over` steps. Rewritten with the regexes above.
  * **v2** -- JSON data islands, a selector/filter value grammar, declarative
    charts and bound tables, goal-directed reasoning, and verbatim editorial
    blocks. Located with BeautifulSoup and rewritten by splicing the original
    source at each node's offsets, so untouched markup stays byte-identical.

The paths never mix. The v1 value regex would read `race[last].gap | thousands`
as result `race[last]`, field `gap | thousands` -- corrupting a v2 artifact
silently instead of rejecting it loudly.
"""

from __future__ import annotations

import re
from typing import Protocol

from server import artifact, linter, temporal

# Owner identity is out of scope for this POC.

_TABLE_RE = re.compile(
    r'<table\b[^>]*\bdata-result="([^"]+)"[^>]*>(.*?)</table>',
    re.IGNORECASE | re.DOTALL,
)
_TBODY_RE = re.compile(r"<tbody\b[^>]*>.*?</tbody>", re.IGNORECASE | re.DOTALL)
_THEAD_RE = re.compile(r"<thead\b[^>]*>(.*?)</thead>", re.IGNORECASE | re.DOTALL)
_TH_RE = re.compile(r"<th\b[^>]*>(.*?)</th>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_DATA_VALUE_RE = re.compile(
    r'(<(\w+)\b[^>]*\bdata-value="([^".]+)\.([^"]+)"[^>]*>)(.*?)(</\2>)',
    re.IGNORECASE | re.DOTALL,
)
_DATA_REASONING_RE = re.compile(
    r'(<(\w+)\b[^>]*\bdata-reasoning="([^"]+)"[^>]*>)(.*?)(</\2>)',
    re.IGNORECASE | re.DOTALL,
)
_DATA_RESULT_NAMES = re.compile(r'data-result="([^"]+)"', re.IGNORECASE)
_DATA_VALUE_REFS = re.compile(r'data-value="([^"]+)"', re.IGNORECASE)
_OVER_ATTR = re.compile(r'data-over="([^".]+)\.([^"]+)"', re.IGNORECASE)
_AGG_ATTR = re.compile(r'data-agg="([^"]+)"', re.IGNORECASE)

_EMPTY_CATALOG = {"metrics": set(), "value_sets": {}, "dimensions": {}}


class Distiller(Protocol):
    """One-method interface so an LLM distiller can replace the heuristic."""

    def distill(self, session_record: dict) -> dict: ...


def _latest_by_name(log_rows: list[dict]) -> dict[str, str]:
    """Map each result_name to its most recent SQL text (later calls win)."""
    latest: dict[str, str] = {}
    for row in log_rows:
        name = row.get("result_name")
        if name:
            latest[name] = row["sql_text"]
    return latest


def _text(html: str) -> str:
    return _TAG_RE.sub("", html).strip()


def _table_headers(inner: str) -> list[str]:
    thead = _THEAD_RE.search(inner)
    if not thead:
        return []
    return [_text(th) for th in _TH_RE.findall(thead.group(1))]


def _loop_for(name: str) -> str:
    return (
        "<tbody>\n"
        "{% for __row in " + name + ".rows %}"
        "<tr>{% for __col in " + name + ".columns %}"
        "<td>{{ __row[__col] }}</td>{% endfor %}</tr>\n"
        "{% endfor %}</tbody>"
    )


def _reasoning_meta(open_tag: str) -> tuple[str, str, str] | None:
    over = _OVER_ATTR.search(open_tag)
    if not over:
        return None
    agg_match = _AGG_ATTR.search(open_tag)
    agg = agg_match.group(1) if agg_match else "max"
    return over.group(1), over.group(2), agg


def _referenced_names(html: str) -> list[str]:
    referenced: list[str] = []

    def _add(name: str) -> None:
        if name and name not in referenced:
            referenced.append(name)

    for name in _DATA_RESULT_NAMES.findall(html):
        _add(name)
    for ref in _DATA_VALUE_REFS.findall(html):
        _add(ref.split(".", 1)[0])
    for match in _DATA_REASONING_RE.finditer(html):
        meta = _reasoning_meta(match.group(1))
        if meta:
            _add(meta[0])
    return referenced


def _field_pairs(html: str, matched: set[str]) -> list[tuple[str, str]]:
    """Collect (result_name, field) pairs from table headers and data-value refs."""
    pairs: list[tuple[str, str]] = []

    def _add(result_name: str, field: str) -> None:
        pair = (result_name, field)
        if result_name in matched and field and pair not in pairs:
            pairs.append(pair)

    for name, inner in _TABLE_RE.findall(html):
        for header in _table_headers(inner):
            _add(name, header)
    for ref in _DATA_VALUE_REFS.findall(html):
        if "." in ref:
            result_name, field = ref.split(".", 1)
            _add(result_name, field)
    return pairs


def _metric_bindings(pairs: list[tuple[str, str]], catalog: dict) -> list[dict]:
    bindings: list[dict] = []
    for result_name, field in pairs:
        if field in catalog["metrics"]:
            bindings.append(
                {
                    "result_name": result_name,
                    "field": field,
                    "metric_id": field,
                    "value_set": None,
                }
            )
        elif field in catalog["dimensions"]:
            bindings.append(
                {
                    "result_name": result_name,
                    "field": field,
                    "metric_id": None,
                    "value_set": catalog["dimensions"][field],
                }
            )
    return bindings


def _build_template(
    html: str,
    matched: set[str],
    unreplayable: list[str],
) -> tuple[str, list[dict]]:
    """Rewrite tables, data-value spans, and data-reasoning elements into Jinja."""
    reasoning_steps: list[dict] = []

    def _table_sub(match: re.Match[str]) -> str:
        name, inner, whole = match.group(1), match.group(2), match.group(0)
        if name not in matched:
            return whole
        if not _TBODY_RE.search(inner):
            unreplayable.append(
                f"table '{name}' has no <tbody> to templatize; left static"
            )
            return whole
        new_inner = _TBODY_RE.sub(lambda _m: _loop_for(name), inner, count=1)
        return whole.replace(inner, new_inner, 1)

    def _value_sub(match: re.Match[str]) -> str:
        open_tag, _tag, result_name, field, _text_, close_tag = match.groups()
        if result_name not in matched:
            unreplayable.append(
                f"value '{result_name}.{field}' has no matching query; left static"
            )
            return match.group(0)
        expr = "{{ " + result_name + ".rows[0]['" + field + "'] }}"
        return f"{open_tag}{expr}{close_tag}"

    def _reasoning_sub(match: re.Match[str]) -> str:
        open_tag, _tag, step_id, _text_, close_tag = match.groups()
        meta = _reasoning_meta(open_tag)
        if meta is None:
            unreplayable.append(
                f"reasoning '{step_id}' is missing a data-over attribute; left static"
            )
            return match.group(0)
        result_name, field, agg = meta
        if result_name not in matched:
            unreplayable.append(
                f"reasoning '{step_id}' references unmatched result "
                f"'{result_name}'; left static"
            )
            return match.group(0)
        reasoning_steps.append(
            {
                "step_id": step_id,
                "result_name": result_name,
                "field": field,
                "agg": agg,
            }
        )
        expr = "{{ reasoning['" + step_id + "'] }}"
        return f"{open_tag}{expr}{close_tag}"

    templated = _TABLE_RE.sub(_table_sub, html)
    templated = _DATA_VALUE_RE.sub(_value_sub, templated)
    templated = _DATA_REASONING_RE.sub(_reasoning_sub, templated)
    return templated, reasoning_steps


def distill(
    report_name: str,
    transcript: list[dict],
    final_artifact: dict,
    log_rows: list[dict],
    anchor_date: str,
    catalog: dict | None = None,
    temporal_confirmations: list[dict] | None = None,
) -> dict:
    """Produce a candidate definition from the session record.

    Dispatches on the artifact's markers. The two paths never mix: the v1
    `data-value` regex reads `race[last].gap | thousands` as result
    `race[last]`, field `gap | thousands`, so a v2 artifact that leaked into the
    legacy path would be silently corrupted rather than loudly rejected.
    """
    html = final_artifact.get("content", "")
    distiller = _distill_v2 if artifact.is_v2(html) else _distill_legacy
    return distiller(
        report_name,
        transcript,
        final_artifact,
        log_rows,
        anchor_date,
        catalog,
        temporal_confirmations,
    )


def _distill_legacy(
    report_name: str,
    transcript: list[dict],
    final_artifact: dict,
    log_rows: list[dict],
    anchor_date: str,
    catalog: dict | None = None,
    temporal_confirmations: list[dict] | None = None,
) -> dict:
    """The v1 contract: populated tables, `result.field` values, data-over steps."""
    catalog = catalog or _EMPTY_CATALOG
    html = final_artifact.get("content", "")
    latest = _latest_by_name(log_rows)

    referenced = _referenced_names(html)
    unreplayable: list[str] = []
    for name in referenced:
        if name not in latest:
            unreplayable.append(
                f"reference '{name}' has no matching query in the tool-call log"
            )

    included = [name for name in referenced if name in latest]
    matched = set(included)
    warnings: list[str] = []
    parameterized_sql: list[dict] = []
    for name in included:
        new_sql, sql_warnings = temporal.reparameterize(
            latest[name], anchor_date, temporal_confirmations
        )
        warnings.extend(sql_warnings)
        parameterized_sql.append({"result_name": name, "sql": new_sql})

    metric_bindings = _metric_bindings(_field_pairs(html, matched), catalog)
    template, reasoning_steps = _build_template(html, matched, unreplayable)
    formats = [fmt.lower() for fmt in final_artifact.get("formats", ["html"])]
    if "html" not in formats:
        formats = ["html"] + formats

    return {
        "report_name": report_name,
        "anchor_date": anchor_date,
        "parameterized_sql": parameterized_sql,
        "metric_bindings": metric_bindings,
        "reasoning_steps": reasoning_steps,
        "rendering_spec": {
            "title": final_artifact.get("title", report_name),
            "template": template,
            "formats": formats,
        },
        "warnings": warnings,
        "unreplayable_sections": unreplayable,
    }


# ---------------------------------------------------------------------------
# The v2 contract: data islands, a selector/filter value grammar, declarative
# charts and tables, goal-directed reasoning, and verbatim editorial blocks.
# ---------------------------------------------------------------------------

_TABBED_LAYOUT = "tabbed-dashboard"

_SINGLE_QUOTE = "'"


def _jinja_string(text: str) -> str:
    """A single-quoted Jinja string literal.

    Always single-quoted: a `sign_class(...)` call gets spliced into a
    `class="..."` attribute, where a double quote would end the attribute early.
    """
    escaped = text.replace("\\", "\\\\").replace(_SINGLE_QUOTE, "\\" + _SINGLE_QUOTE)
    return f"{_SINGLE_QUOTE}{escaped}{_SINGLE_QUOTE}"


def _selector_literal(selector: tuple) -> str:
    if selector[0] == "index":
        index = selector[1]
        return _jinja_string("[last]" if index == -1 else f"[{index}]")
    _kind, column, value = selector
    doubled = value.replace(_SINGLE_QUOTE, _SINGLE_QUOTE * 2)
    return _jinja_string(f"[{column}={_SINGLE_QUOTE}{doubled}{_SINGLE_QUOTE}]")


def _pick_expr(ref) -> str:
    return (
        f"pick({ref.result}, {_selector_literal(ref.selector)}, "
        f"{_jinja_string(ref.field)})"
    )


def _filter_suffix(filters) -> str:
    parts = [f"{name}({args[0]})" if args else name for name, args in filters]
    return "".join(f" | {part}" for part in parts)


_CLASS_ATTR_RE = re.compile(r'\bclass="([^"]*)"')
_OPEN_TAG_NAME_RE = re.compile(r"^<([a-zA-Z][\w:-]*)")


def _with_sign_class(open_tag: str, expr: str) -> str:
    """Merge sign_class(...) into the element's class list, never replacing it."""
    injected = "{{ sign_class(" + expr + ") }}"
    match = _CLASS_ATTR_RE.search(open_tag)
    if match:
        existing = match.group(1).strip()
        merged = f"{existing} {injected}" if existing else injected
        return open_tag[: match.start(1)] + merged + open_tag[match.end(1) :]
    cut = _OPEN_TAG_NAME_RE.match(open_tag).end()
    return f'{open_tag[:cut]} class="{injected}"{open_tag[cut:]}'


def apply_splices(html: str, splices: list[tuple[int, int, str]]) -> str:
    """Replace [start, end) spans, right to left so earlier edits keep offsets valid.

    Everything outside a splice is copied verbatim. That is what keeps untouched
    markup byte-for-byte identical and the editorial hashes stable.

    Shared with `server/normalizer.py`, which rewrites a free-form artifact into
    the v2 contract by exactly this technique.
    """
    out = html
    for start, end, replacement in sorted(splices, key=lambda s: s[0], reverse=True):
        out = out[:start] + replacement + out[end:]
    return out


# The private name predates the normalizer; kept so existing call sites read the same.
_apply_splices = apply_splices


def _watch_to_json(watch: dict) -> dict:
    """Flatten the parsed watch into something json.dumps can store."""
    ref = watch["ref"]
    return {
        "raw": watch["raw"],
        "result": ref.result,
        "selector": list(ref.selector),
        "field": ref.field,
        "op": watch["op"],
        "value": watch["value"],
    }


def _build_template_v2(
    html: str,
    model,
    matched: set[str],
    unreplayable: list[str],
) -> tuple[str, list[dict], list[dict]]:
    splices: list[tuple[int, int, str]] = []

    for name, span in model.island_spans.items():
        if name not in matched:
            unreplayable.append(f"island '{name}' has no matching query; left static")
            continue
        splices.append(
            (span.open_end, span.close_start, "{{ " + name + ".rows | tojson }}")
        )

    for ref in model.value_refs:
        if ref.result not in matched:
            unreplayable.append(f"value '{ref.raw}' has no matching query; left static")
            continue
        expr = _pick_expr(ref)
        splices.append(
            (
                ref.span.open_end,
                ref.span.close_start,
                "{{ " + expr + _filter_suffix(ref.filters) + " }}",
            )
        )
        if ref.style == "sign":
            open_tag = html[ref.span.outer_start : ref.span.open_end]
            splices.append(
                (ref.span.outer_start, ref.span.open_end, _with_sign_class(open_tag, expr))
            )

    reasoning_steps: list[dict] = []
    for step in model.reasoning_steps:
        missing = sorted(
            {
                inp["result_name"]
                for inp in step["inputs"]
                if inp["result_name"] not in matched
            }
        )
        if missing:
            unreplayable.append(
                f"reasoning '{step['step_id']}' references unmatched result(s) "
                f"{', '.join(missing)}; left static"
            )
            continue
        span = step["span"]
        splices.append(
            (span.open_end, span.close_start, "{{ reasoning['" + step["step_id"] + "'] }}")
        )
        reasoning_steps.append(
            {
                "step_id": step["step_id"],
                "goal": step["goal"],
                "inputs": step["inputs"],
                "max_sentences": step["max_sentences"],
                "style": step["style"],
            }
        )

    for step in model.legacy_reasoning:
        unreplayable.append(
            f"reasoning '{step['step_id']}' uses the v1 data-over form inside a v2 "
            "artifact; left static"
        )

    # Editorial prose is never rewritten -- that is the point of the content class.
    # A banner slot goes immediately before it, empty unless the runner's watch
    # evaluation flags the block at replay time.
    editorial_blocks: list[dict] = []
    for block in model.editorial_blocks:
        start = block["span"].outer_start
        splices.append(
            (start, start, "{{ editorial_banner('" + block["block_id"] + "') }}")
        )
        editorial_blocks.append(
            {
                "block_id": block["block_id"],
                "html_sha256": block["html_sha256"],
                "authored_as_of": block["authored_as_of"],
                "watch": _watch_to_json(block["watch"]) if block["watch"] else None,
            }
        )

    return _apply_splices(html, splices), reasoning_steps, editorial_blocks


def _field_pairs_v2(model, matched: set[str]) -> list[tuple[str, str]]:
    """Every island column, plus every field a data-value names.

    Bound-table headers are excluded on purpose: they are display labels
    ("Gap to #1"), not field names, and would bind to nothing.
    """
    pairs: list[tuple[str, str]] = []

    def add(result_name: str, field: str) -> None:
        pair = (result_name, field)
        if result_name in matched and field and pair not in pairs:
            pairs.append(pair)

    for name, rows in model.islands.items():
        if rows:
            for column in rows[0]:
                add(name, column)
    for ref in model.value_refs:
        add(ref.result, ref.field)
    return pairs


def _inject_missing_islands(template: str, model, matched: set) -> str:
    """Supply a data island for any bound table or chart that lacks one.

    A bound table and a chart are filled/drawn by the runtime from a JSON data
    island (`<script type="application/json" data-result="X">`). Declaring the
    table or chart without also embedding its result as an island is easy to
    forget, and the parity gate does not catch it -- the reference still resolves
    to a logged query -- so the table renders empty in the browser. Emit the
    island here so the runtime always has data to read. Parity is unaffected: it
    compares the artifact's own islands, and the injected rows come from the same
    query it runs.
    """
    have = set(model.islands)
    consumers = [t["result"] for t in model.bound_tables]
    consumers += [c.get("result") for c in model.charts if c.get("result")]
    missing = [n for n in dict.fromkeys(consumers) if n in matched and n not in have]
    islands = "".join(
        f'<script type="application/json" data-result="{n}">'
        f"{{{{ {n}.rows | tojson }}}}</script>"
        for n in missing
    )
    return islands + template if islands else template


def _distill_v2(
    report_name: str,
    transcript: list[dict],
    final_artifact: dict,
    log_rows: list[dict],
    anchor_date: str,
    catalog: dict | None = None,
    temporal_confirmations: list[dict] | None = None,
) -> dict:
    """The v2 contract. Same four-part definition, plus editorial blocks."""
    catalog = catalog or _EMPTY_CATALOG
    html = final_artifact.get("content", "")
    latest = _latest_by_name(log_rows)

    model = artifact.parse(html)
    unreplayable: list[str] = list(model.problems)
    lint_unreplayable, warnings = linter.lint(html)
    unreplayable.extend(lint_unreplayable)

    referenced = model.referenced_names()
    for name in referenced:
        if name not in latest:
            unreplayable.append(
                f"reference '{name}' has no matching query in the tool-call log"
            )

    included = [name for name in referenced if name in latest]
    matched = set(included)

    parameterized_sql: list[dict] = []
    for name in included:
        new_sql, sql_warnings = temporal.reparameterize(
            latest[name], anchor_date, temporal_confirmations
        )
        warnings.extend(sql_warnings)
        parameterized_sql.append({"result_name": name, "sql": new_sql})

    metric_bindings = _metric_bindings(_field_pairs_v2(model, matched), catalog)
    template, reasoning_steps, editorial_blocks = _build_template_v2(
        html, model, matched, unreplayable
    )
    template = _inject_missing_islands(template, model, matched)

    formats = [fmt.lower() for fmt in final_artifact.get("formats", ["html"])]
    if "html" not in formats:
        formats = ["html"] + formats
    layout = final_artifact.get("layout")
    if layout == _TABBED_LAYOUT and formats != ["html"]:
        # Markdown has no tabs, no SVG, and no runtime to fill the bound tables.
        warnings.append(
            "layout 'tabbed-dashboard' renders HTML only; markdown output skipped"
        )
        formats = ["html"]

    return {
        "report_name": report_name,
        "anchor_date": anchor_date,
        "parameterized_sql": parameterized_sql,
        "metric_bindings": metric_bindings,
        "reasoning_steps": reasoning_steps,
        "editorial_blocks": editorial_blocks,
        "rendering_spec": {
            "title": final_artifact.get("title", report_name),
            "template": template,
            "formats": formats,
            "layout": layout,
            "theme": final_artifact.get("theme"),
            "sections": model.tabs or [],
            "charts": model.charts,
            "tables": model.bound_tables,
        },
        "warnings": warnings,
        "unreplayable_sections": unreplayable,
    }
