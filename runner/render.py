"""Rendering shared by the parity gate and the runner.

The definition's rendering_spec holds a body template (the artifact HTML with
each table's rows replaced by a loop, data-value numbers replaced by expressions,
and data-reasoning elements replaced by narrative expressions). Rendering runs
that body template against the named results and the reasoning narratives, then
wraps the output in the base page template. The same spec also produces Markdown,
so both formats come from one rendering spec.

A v2 definition additionally carries JSON data islands, declarative charts and
bound tables, a theme, and a tabbed layout. Islands render through Jinja's
``tojson``; charts and tables stay empty markup that ``charts_v1.js`` fills in the
browser. That runtime only draws and formats -- every number it shows was computed
by SQL.

Convenience builders let the client construct a parity-safe artifact: tables with
data-result attributes, headline spans with data-value attributes, and reasoning
placeholders with data-reasoning attributes.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from decimal import Decimal
from html import escape

from jinja2 import Environment, FileSystemLoader, pass_context, select_autoescape
from jinja2.utils import htmlsafe_json_dumps
from markupsafe import Markup

from server.db import TEMPLATES_DIR

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "j2"]),
)


class SelectorError(ValueError):
    """A `pick(...)` selector does not resolve against the fresh results."""


def _json_default(obj: object) -> object:
    """Teach `tojson` the scalar types DuckDB hands back.

    DuckDB returns `datetime.date` for DATE columns and `Decimal` for some
    aggregates; json.dumps raises TypeError on both. Anything else still raises,
    because a column that cannot survive the island round-trip should fail loudly
    at render time rather than silently disappear from the report.
    """
    if isinstance(obj, (dt.date, dt.datetime)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"{type(obj).__name__} is not island-serializable")


# Jinja's built-in `tojson` reads these; it still returns Markup and still escapes
# <, >, & and ' as \uXXXX, so an island can never break out of its <script> tag.
_env.policies["json.dumps_kwargs"] = {"sort_keys": False, "default": _json_default}


# --- value filters -------------------------------------------------------
#
# Filters format; they never derive. Each one tolerates the output of the others,
# so `| signed | thousands` and `| thousands | signed` both produce "+1,234".
# `round` is deliberately absent: Jinja's built-in already does `| round(2)`, and
# shadowing it would change the legacy path.


def _to_number(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("booleans are not numbers")
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    text = str(value).strip().replace(",", "").lstrip("+")
    for suffix in ("pp", "%"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    return float(text)


def _already_signed(value: object) -> bool:
    return isinstance(value, str) and value.strip()[:1] in "+-"


def do_thousands(value: object) -> str:
    """1234 -> '1,234'. Preserves a sign a previous filter already added."""
    number = _to_number(value)
    if float(number).is_integer():
        number = int(number)
    text = f"{number:,}"
    if number > 0 and _already_signed(value):
        text = f"+{text}"
    return text


def do_signed(value: object) -> str:
    """Prefix a '+' on positive values; negatives already carry their sign."""
    number = _to_number(value)
    if isinstance(value, str):
        text = value.strip()
    else:
        text = str(int(number) if float(number).is_integer() else number)
    if number > 0 and not _already_signed(text):
        return f"+{text}"
    return text


def do_pct(value: object, decimals: int = 1) -> str:
    """34.52 -> '34.5%'. The percentage was computed in SQL, not here."""
    text = f"{_to_number(value):.{decimals}f}%"
    if _to_number(value) > 0 and _already_signed(value):
        text = f"+{text}"
    return text


def do_pp(value: object) -> str:
    """-2.7 -> '-2.7pp' (percentage points)."""
    if isinstance(value, str):
        return f"{value.strip()}pp"
    number = _to_number(value)
    if float(number).is_integer():
        number = int(number)
    return f"{number}pp"


_env.filters["thousands"] = do_thousands
_env.filters["signed"] = do_signed
_env.filters["pct"] = do_pct
_env.filters["pp"] = do_pp


# --- value globals -------------------------------------------------------

_SEL_INDEX = re.compile(r"^\[(\d+|first|last)\]$")
_SEL_MATCH = re.compile(r"^\[([A-Za-z_]\w*)\s*=\s*'((?:[^']|'')*)'\]$")


def pick(result: dict, selector: str, field: str) -> object:
    """Pull one field out of one row of a named result.

    Selectors mirror the artifact's data-value grammar: `'.'` (row 0), `'[3]'`,
    `'[first]'`, `'[last]'`, `"[col='val']"`.

    Raises rather than returning a blank. A silent blank would render identically
    on both sides of the parity gate, letting a broken report pass.
    """
    rows = result["rows"] if isinstance(result, dict) else None
    if rows is None:
        raise SelectorError(f"pick: {field!r} referenced a result with no rows key")
    if not rows:
        raise SelectorError(f"pick: no rows to select {field!r} from")

    if selector == ".":
        row = rows[0]
    elif match := _SEL_INDEX.match(selector):
        token = match.group(1)
        index = 0 if token == "first" else -1 if token == "last" else int(token)
        try:
            row = rows[index]
        except IndexError:
            raise SelectorError(
                f"pick: index {token} is out of range ({len(rows)} rows)"
            ) from None
    elif match := _SEL_MATCH.match(selector):
        column, value = match.group(1), match.group(2).replace("''", "'")
        row = next((r for r in rows if str(r.get(column)) == value), None)
        if row is None:
            raise SelectorError(f"pick: no row where {column}={value!r}")
    else:
        raise SelectorError(f"pick: unrecognized selector {selector!r}")

    if field not in row:
        raise SelectorError(f"pick: field {field!r} not in row; have {list(row)}")
    return row[field]


def selector_literal(selector: tuple | list) -> str:
    """Turn a stored selector back into the string `pick` accepts.

    The compiler stores `("index", -1)` / `("match", col, val)` in the definition;
    the runner needs `[last]` / `[col='val']` to resolve a watch condition.
    """
    kind = selector[0]
    if kind == "index":
        index = selector[1]
        return "[last]" if index == -1 else f"[{index}]"
    if kind == "match":
        doubled = str(selector[2]).replace("'", "''")
        return f"[{selector[1]}='{doubled}']"
    raise SelectorError(f"unrecognized stored selector {selector!r}")


def sign_class(value: object) -> str:
    """CSS class for a delta: growth / decline / flat."""
    try:
        number = _to_number(value)
    except (TypeError, ValueError):
        return "flat"
    return "growth" if number > 0 else "decline" if number < 0 else "flat"


@pass_context
def editorial_banner(context, block_id: str) -> Markup:
    """Nothing, unless the runner flagged this block's watch condition.

    Returns Markup: the environment autoescapes string templates, so a plain str
    would render the banner's own tags as visible text.
    """
    stale = (context.get("stale_blocks") or {}).get(block_id)
    if not stale:
        return Markup("")
    return Markup(
        '<div class="staleness-banner" role="status">'
        f"<strong>Editorial note may be out of date.</strong> {escape(str(stale))}"
        "</div>"
    )


_env.globals["pick"] = pick
_env.globals["sign_class"] = sign_class
_env.globals["editorial_banner"] = editorial_banner

_TAG_RE = re.compile(r"<[^>]+>")
_TABLE_RE = re.compile(
    r'<table\b[^>]*\bdata-result="([^"]+)"[^>]*>(.*?)</table>',
    re.IGNORECASE | re.DOTALL,
)
_THEAD_RE = re.compile(r"<thead\b[^>]*>(.*?)</thead>", re.IGNORECASE | re.DOTALL)
_TBODY_RE = re.compile(r"<tbody\b[^>]*>(.*?)</tbody>", re.IGNORECASE | re.DOTALL)
_TH_RE = re.compile(r"<th\b[^>]*>(.*?)</th>", re.IGNORECASE | re.DOTALL)
_TD_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
_TR_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_HEADING_RE = re.compile(r"<h([1-3])\b[^>]*>(.*?)</h\1>", re.IGNORECASE | re.DOTALL)
_PARA_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)


TABBED_LAYOUT = "tabbed-dashboard"
_DEFAULT_THEME = "market_story_v1"


def _render_body(
    definition: dict,
    results_by_name: dict,
    reasoning: dict,
    stale_blocks: dict[str, str] | None = None,
) -> str:
    template = _env.from_string(definition["rendering_spec"]["template"])
    return template.render(
        reasoning=reasoning or {},
        stale_blocks=stale_blocks or {},
        **results_by_name,
    )


def _title(definition: dict) -> str:
    return definition["rendering_spec"].get("title") or definition.get(
        "report_name", "Report"
    )


def _read_asset(kind: str, name: str, suffix: str) -> str:
    path = TEMPLATES_DIR / kind / f"{name}{suffix}"
    if not path.exists():
        raise FileNotFoundError(f"{kind[:-1]} asset not found: {path}")
    return path.read_text(encoding="utf-8")


def _split_sections(body_html: str, sections: list[dict]) -> tuple[str, dict[str, str]]:
    """Group top-level body elements into tab panels by their `data-section`.

    Anything without a `data-section` (the KPI strip, the islands) stays above the
    tab bar, where it is visible from every panel and reachable by the runtime.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(body_html, "html.parser")
    panels: dict[str, list[str]] = {section["id"]: [] for section in sections}
    prelude: list[str] = []
    for node in soup.contents:
        section_id = (
            node.get("data-section") if hasattr(node, "get") else None
        )
        if section_id in panels:
            panels[section_id].append(str(node))
        else:
            prelude.append(str(node))
    return "".join(prelude), {key: "".join(value) for key, value in panels.items()}


def render_html(
    definition: dict,
    results_by_name: dict[str, dict],
    reasoning: dict[str, str] | None = None,
    as_of: str | None = None,
    stale_blocks: dict[str, str] | None = None,
) -> str:
    """Render the definition to a full HTML page."""
    spec = definition["rendering_spec"]
    body_html = _render_body(definition, results_by_name, reasoning or {}, stale_blocks)

    if spec.get("layout") != TABBED_LAYOUT:
        base = _env.get_template("report_base.html.j2")
        return base.render(title=_title(definition), body=body_html)

    sections = spec.get("sections") or []
    prelude, panels = _split_sections(body_html, sections)
    layout = _env.get_template("layouts/tabbed_dashboard.html.j2")
    theme = (spec.get("theme") or _DEFAULT_THEME).replace("-", "_")
    return layout.render(
        title=_title(definition),
        prelude=prelude,
        sections=sections,
        panels=panels,
        theme_css=_read_asset("themes", theme, ".css"),
        runtime_js=_read_asset("runtime", "charts_v1", ".js"),
        as_of=as_of,
        chart_count=len(spec.get("charts") or []),
    )


def render_markdown(
    definition: dict,
    results_by_name: dict[str, dict],
    reasoning: dict[str, str] | None = None,
    stale_blocks: dict[str, str] | None = None,
) -> str:
    """Render the definition to Markdown from the same rendering spec."""
    body_html = _render_body(definition, results_by_name, reasoning or {}, stale_blocks)
    md_body = _html_to_markdown(body_html)
    if md_body.lstrip().startswith("# "):
        # The body already carries its own top-level heading.
        return md_body
    return f"# {_title(definition)}\n\n{md_body}"


def render(
    definition: dict,
    results_by_name: dict[str, dict],
    reasoning: dict[str, str] | None = None,
    fmt: str = "html",
    as_of: str | None = None,
    stale_blocks: dict[str, str] | None = None,
) -> str:
    """Render to the requested format ('html' or 'md')."""
    if fmt == "md":
        return render_markdown(definition, results_by_name, reasoning, stale_blocks)
    if fmt == "html":
        return render_html(definition, results_by_name, reasoning, as_of, stale_blocks)
    raise ValueError(f"Unsupported render format: {fmt}")


def _text(html: str) -> str:
    return _TAG_RE.sub("", html).strip()


def _table_to_markdown(inner: str) -> str:
    thead = _THEAD_RE.search(inner)
    headers = [_text(th) for th in _TH_RE.findall(thead.group(1))] if thead else []
    body = _TBODY_RE.search(inner)
    rows_html = body.group(1) if body else inner
    rows = []
    for tr in _TR_RE.findall(rows_html):
        cells = [_text(td) for td in _TD_RE.findall(tr)]
        if cells:
            rows.append(cells)
    if not headers and rows:
        headers = [f"col{i + 1}" for i in range(len(rows[0]))]
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    for cells in rows:
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _html_to_markdown(html: str) -> str:
    """Convert the controlled artifact HTML subset to Markdown."""
    blocks: list[str] = []
    pos = 0
    # Walk headings, paragraphs, and tables in document order.
    pattern = re.compile(
        r"<h([1-3])\b[^>]*>(.*?)</h\1>"
        r"|<p\b[^>]*>(.*?)</p>"
        r"|<table\b[^>]*\bdata-result=\"[^\"]+\"[^>]*>(.*?)</table>",
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(html):
        _ = pos
        if match.group(1):  # heading
            level = int(match.group(1))
            blocks.append("#" * level + " " + _text(match.group(2)))
        elif match.group(3) is not None:  # paragraph
            text = _text(match.group(3))
            if text:
                blocks.append(text)
        else:  # table
            blocks.append(_table_to_markdown(match.group(4)))
    return "\n\n".join(blocks) + "\n"


def build_table_html(result_name: str, columns: list[str], rows: list[dict]) -> str:
    """Build a data-result table from a query result (client convenience)."""
    head = "".join(f"<th>{escape(str(c))}</th>" for c in columns)
    body_rows = []
    for row in rows:
        cells = "".join(f"<td>{escape(str(row[c]))}</td>" for c in columns)
        body_rows.append(f"<tr>{cells}</tr>")
    return (
        f'<table data-result="{escape(result_name)}">'
        f"<thead><tr>{head}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody>"
        f"</table>"
    )


def build_value_span(result_name: str, field: str, value: object) -> str:
    """Build a data-value headline span (client convenience)."""
    ref = f"{result_name}.{field}"
    return f'<span data-value="{escape(ref)}">{escape(str(value))}</span>'


def build_reasoning_para(
    step_id: str, result_name: str, field: str, agg: str = "max"
) -> str:
    """Build a data-reasoning placeholder the runner fills with narrative."""
    over = f"{result_name}.{field}"
    return (
        f'<p data-reasoning="{escape(step_id)}" data-over="{escape(over)}" '
        f'data-agg="{escape(agg)}"></p>'
    )


# --- v2 artifact builders ------------------------------------------------
#
# The client builds islands and declarations with these so the artifact matches
# what server/artifact.py parses. Values go in verbatim: the parity gate compares
# what SQL returned against what the template re-renders.


def build_island(result_name: str, result: dict) -> str:
    """Embed a query result as a JSON data island the runtime can read.

    Serialized through the very function Jinja's `tojson` calls, so the island the
    client submits and the island the compiled template re-renders are byte
    identical. That is what lets the parity gate compare the two at all.
    """
    payload = str(htmlsafe_json_dumps(result["rows"], default=_json_default))
    return (
        f'<script type="application/json" data-result="{escape(result_name)}">'
        f"{payload}</script>"
    )


def build_value_span_v2(
    ref: str, value: object, style: str | None = None, css_class: str | None = None
) -> str:
    """A headline number bound to `result[selector].field | filters`."""
    attrs = f' data-value="{escape(ref)}"'
    if style:
        attrs += f' data-style="{escape(style)}"'
    if css_class:
        attrs += f' class="{escape(css_class)}"'
    return f"<span{attrs}>{escape(str(value))}</span>"


def build_chart_div(spec: dict, element_id: str | None = None) -> str:
    """A chart placeholder. charts_v1.js draws into it; it derives nothing."""
    ident = f' id="{escape(element_id)}"' if element_id else ""
    return f'<div{ident} class="chart" data-chart=\'{json.dumps(spec)}\'></div>'


def build_bound_table(result_name: str, columns: list[dict]) -> str:
    """A table whose <tbody> the runtime fills from the named island.

    `columns` is a list of {field, header, filters?, style?}. The tbody must stay
    empty: the parity gate never runs JavaScript, so a populated row here would
    be compared against nothing.
    """
    specs = []
    headers = []
    for column in columns:
        parts = [f"{column['field']}:{column['header']}"]
        for name, args in column.get("filters") or []:
            parts.append(f"{name}({args[0]})" if args else name)
        if column.get("style"):
            parts.append(f"style:{column['style']}")
        specs.append("|".join(parts))
        headers.append(f"<th>{escape(str(column['header']))}</th>")
    return (
        f'<table data-result="{escape(result_name)}" '
        f'data-columns="{escape(", ".join(specs))}">'
        f"<thead><tr>{''.join(headers)}</tr></thead><tbody></tbody></table>"
    )


def build_reasoning_block(
    step_id: str,
    goal: str,
    inputs: list[str],
    max_sentences: int = 3,
    style: str | None = None,
) -> str:
    """A v2 reasoning placeholder: state the goal, name the inputs."""
    attrs = (
        f'data-reasoning="{escape(step_id)}" data-goal="{escape(goal)}" '
        f'data-inputs="{escape(", ".join(inputs))}" '
        f'data-max-sentences="{max_sentences}"'
    )
    if style:
        attrs += f' data-style="{escape(style)}"'
    return f"<p {attrs}></p>"


def build_editorial_block(
    block_id: str,
    html: str,
    authored_as_of: str,
    watch: str | None = None,
) -> str:
    """Author-written prose, replayed verbatim and hashed.

    `html` is inserted as given -- that is the point of the content class. A watch
    condition lets the runner flag it when the numbers move out from under it.
    """
    attrs = (
        f'data-editorial="{escape(block_id)}" '
        f'data-authored-as-of="{escape(authored_as_of)}"'
    )
    if watch:
        attrs += f' data-watch="{escape(watch)}"'
    return f'<div class="editorial" {attrs}>{html}</div>'


def build_tabs(sections: list[dict]) -> str:
    """The tab declaration; the layout builds the bar and panels from it."""
    return f"<nav data-tabs='{json.dumps(sections)}'></nav>"
