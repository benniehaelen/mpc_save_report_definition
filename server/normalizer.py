"""Rewrite a free-form artifact into a v2-contract artifact.

This is a front-end normalizer, not a second compiler. It applies a validated
`NormalizationPlan` to the submitted HTML and hands the result to the existing,
proven pipeline: `artifact.parse` -> `compiler._distill_v2` -> `linter.lint` ->
`parity.check`. Nothing downstream knows the artifact was ever free-form.

Every edit is a **splice at recorded source offsets**, applied right-to-left by
`compiler.apply_splices`. Untouched markup stays byte-for-byte identical, which
is what keeps a page's own CSS, SVG, and prose intact -- and what keeps editorial
`html_sha256` values stable.

## What each plan item becomes

* **A matched blob** becomes a JSON data island carrying the **logged rows**, not
  the blob's own values. The logged rows are ground truth; a projection match
  gets the *full* result rows. The original `const NAME = [...]` is repointed to
  `const NAME = __ISLAND__('result_name')`, so the page's existing drawing code
  keeps working and redraws from fresh data at replay.
* **A resolved scalar** gains a `data-value` attribute on the element that already
  holds it -- exactly one splice per number, because parity compares `data-value`
  counts and order positionally.
* **Prose** becomes editorial (frozen, dated, hashed), analytical (emptied, with a
  goal for the reasoning engine to fill), or computed (left alone; its numbers are
  already bound).

## `__ISLAND__`

Injected into the artifact, not added to `templates/runtime/charts_v1.js`. The
runtime is only inlined for the tabbed-dashboard layout, and a free-form page
renders through the default layout. The helper carries no Jinja delimiters, so the
linter accepts it; it is not `type="application/json"` and has no `data-result`, so
the parser ignores it and the compiler splices straight past it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape

from bs4 import BeautifulSoup

from runner import render
from server import artifact
from server.compiler import apply_splices

# Reads the island the compiler will have rewritten to `{{ name.rows | tojson }}`
# by replay time. Deliberately delimiter-free -- see linter._JINJA_DELIMITER.
ISLAND_HELPER = (
    '<script data-island-helper="1">'
    "function __ISLAND__(n){"
    "var e=document.querySelector('script[type=\"application/json\"][data-result=\"'+n+'\"]');"
    "return e?JSON.parse(e.textContent):[];"
    "}</script>"
)

_TABLE_INTERNAL = ("tr", "td", "th", "thead", "tbody", "tfoot")
_BLOCK_LEVEL = ("div", "section", "article", "main", "body", "p", "blockquote")


@dataclass
class NormalizationSummary:
    islands_written: int = 0
    values_bound: int = 0
    reasoning_blocks: int = 0
    editorial_blocks: int = 0
    charts_emitted: int = 0
    constants_repointed: int = 0
    derived_queries: list[str] = field(default_factory=list)
    tabs: bool = False
    warnings: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)


def _selector_text(selector) -> str:
    kind = selector[0]
    if kind == "index":
        index = selector[1]
        return "[last]" if index == -1 else f"[{index}]"
    _, col, val = selector
    return "[" + col + "='" + str(val).replace("'", "''") + "']"


def _filter_suffix(filters) -> str:
    parts = []
    for name, args in filters or []:
        parts.append(f"{name}({args[0]})" if args else str(name))
    return "".join(f" | {p}" for p in parts)


def value_ref_text(entry: dict) -> str:
    """`race[last].gap | thousands` from a plan value entry."""
    selector = _selector_text(tuple(entry["selector"]))
    body = f"{entry['result']}{selector}.{entry['field']}"
    return body + _filter_suffix(entry.get("filters"))


def _add_attrs(open_tag: str, attrs: dict[str, str]) -> str:
    """Insert attributes into an existing open tag, preserving everything else."""
    rendered = "".join(
        f' {name}="{escape(str(value), quote=True)}"' for name, value in attrs.items()
    )
    if open_tag.endswith("/>"):
        return open_tag[:-2] + rendered + "/>"
    return open_tag[:-1] + rendered + ">"


def _hoist_editorial(tag):
    """An editorial block may not be a table row: the banner div would break it."""
    if tag.name not in _TABLE_INTERNAL:
        return tag
    for ancestor in tag.parents:
        if ancestor.name in _BLOCK_LEVEL:
            return ancestor
        if ancestor.name == "table":
            continue
    return None


def normalize(
    html: str,
    plan: dict,
    logged_rows: dict[str, list[dict]],
    report,
    *,
    save_date: str,
) -> tuple[str, NormalizationSummary]:
    """Apply `plan` to `html`, returning a v2-contract artifact and a summary."""
    summary = NormalizationSummary()
    splices: list[tuple[int, int, str]] = []
    soup = BeautifulSoup(html, "html.parser")
    starts = artifact.line_starts(html)

    # The helper must be defined before any page script that calls it.
    splices.append((0, 0, ISLAND_HELPER))
    islands_markup: list[str] = []

    # 1. Islands. The island carries the logged rows; the constant points at it.
    for entry in plan.get("islands", []):
        name = entry["result_name"]
        rows = logged_rows.get(name)
        if rows is None:
            summary.warnings.append(
                f"island {name!r}: no logged rows for it; left as written"
            )
            continue
        blob = report.blob(entry["blob_id"])
        islands_markup.append(render.build_island(name, {"rows": rows}))
        summary.islands_written += 1
        if blob is not None and blob.value_span:
            start, end = blob.value_span
            splices.append((start, end, f"const {blob.name} = __ISLAND__('{name}');"))
            summary.constants_repointed += 1
        elif blob is not None and blob.kind == "html_table":
            # A hand-written table: replace it with a bound one the runtime fills.
            columns = [{"field": c, "header": c} for c in blob.columns]
            splices.append(
                (
                    blob.span.outer_start,
                    blob.span.outer_end,
                    render.build_bound_table(name, columns),
                )
            )

    if islands_markup:
        splices.append((0, 0, "".join(islands_markup)))

    # 2. Narrative, per tier. Done before values, because a number inside an
    #    editorial or analytical block must NOT also be bound: editorial prose
    #    replays verbatim, and analytical prose is discarded and regenerated.
    prose = {b["block_id"]: b for b in _prose_index(html)}
    frozen: list[artifact.Span] = []
    for entry in plan.get("narrative", []):
        block = prose.get(entry["block_id"])
        if block is None:
            continue
        tier = entry.get("tier", "editorial")
        if tier == "computed":
            continue  # leave it; its numbers become data-value spans below

        if tier == "analytical":
            splices.append(
                (
                    block["span"].outer_start,
                    block["span"].outer_end,
                    render.build_reasoning_block(
                        entry["block_id"],
                        entry.get("goal", ""),
                        entry.get("inputs") or [],
                        entry.get("max_sentences", 3),
                    ),
                )
            )
            frozen.append(block["span"])
            summary.reasoning_blocks += 1
            continue

        tag = block["tag_obj"]
        target = _hoist_editorial(tag)
        if target is None:
            summary.warnings.append(
                f"editorial block {entry['block_id']}: inside a table with no block-level "
                "ancestor to hoist to; left unbound"
            )
            continue
        if target is not tag:
            try:
                span = artifact.outer_span(html, target, starts)
            except artifact.GrammarError:
                continue
        else:
            span = block["span"]
        attrs = {
            "data-editorial": entry["block_id"],
            "data-authored-as-of": entry.get("authored_as_of") or save_date,
        }
        if entry.get("watch"):
            attrs["data-watch"] = entry["watch"]
        open_tag = html[span.outer_start : span.open_end]
        splices.append((span.outer_start, span.open_end, _add_attrs(open_tag, attrs)))
        frozen.append(span)
        summary.editorial_blocks += 1

    # 3. Values: annotate the element that already holds the number.
    for entry in plan.get("values", []):
        span = _span_for_value(report, entry["value_id"])
        if span is None:
            summary.warnings.append(f"value {entry['value_id']}: lost its source span")
            continue
        if _inside_any(span, frozen):
            continue  # frozen prose keeps its literal number
        attrs = {"data-value": value_ref_text(entry)}
        if entry.get("style"):
            attrs["data-style"] = entry["style"]
        open_tag = html[span.outer_start : span.open_end]
        splices.append((span.outer_start, span.open_end, _add_attrs(open_tag, attrs)))
        summary.values_bound += 1

    # 4. Charts, when the plan names the container to replace.
    for spec in plan.get("charts", []):
        element_id = spec.get("replace_element_id")
        if not element_id:
            summary.warnings.append(
                f"chart {spec.get('id')!r}: no replace_element_id; the page keeps its own drawing"
            )
            continue
        target = soup.find(id=element_id)
        if target is None:
            summary.warnings.append(f"chart {spec.get('id')!r}: no element #{element_id}")
            continue
        try:
            span = artifact.outer_span(html, target, starts)
        except artifact.GrammarError:
            continue
        clean = {k: v for k, v in spec.items() if k != "replace_element_id"}
        splices.append(
            (span.outer_start, span.outer_end, render.build_chart_div(clean, element_id))
        )
        summary.charts_emitted += 1

    # 5. Tabs.
    if plan.get("tabs"):
        splices.append((0, 0, render.build_tabs(plan["tabs"])))
        summary.tabs = True

    summary.derived_queries = [d["result_name"] for d in plan.get("derived_queries", [])]
    summary.unmatched = [b.describe() for b in report.unmatched] + [
        f"unresolved number {s.raw_text!r}" for s in report.unresolved_values
    ] + [
        f"ambiguous number {s.raw_text!r} (matches {len(c)} results)"
        for s, c in report.ambiguous_values
    ]
    if report.unparseable:
        summary.unmatched.extend(
            f"const {name} is computed in JavaScript, not returned by a query"
            for name in report.unparseable
        )

    return apply_splices(html, splices), summary


def _inside_any(span: artifact.Span, outers: list[artifact.Span]) -> bool:
    return any(
        o.outer_start <= span.outer_start and span.outer_end <= o.outer_end
        for o in outers
    )


def _span_for_value(report, value_id):
    for match in report.value_matches:
        if match.value_id == value_id:
            return match.span
    for scalar in report.scalars:
        if scalar.value_id == value_id:
            return scalar.span
    return None


def _prose_index(html: str) -> list[dict]:
    """Same walk the extractor used, re-run so spans line up with this html."""
    from server.extractor import MIN_PROSE_CHARS, PROSE_TAGS

    soup = BeautifulSoup(html, "html.parser")
    starts = artifact.line_starts(html)
    blocks: list[dict] = []
    for tag in soup.find_all(PROSE_TAGS):
        if tag.find(PROSE_TAGS):
            continue
        text = tag.get_text(" ", strip=True)
        if len(text) < MIN_PROSE_CHARS:
            continue
        try:
            span = artifact.outer_span(html, tag, starts)
        except artifact.GrammarError:
            continue
        blocks.append(
            {"block_id": f"b{len(blocks)}", "span": span, "tag_obj": tag}
        )
    return blocks
