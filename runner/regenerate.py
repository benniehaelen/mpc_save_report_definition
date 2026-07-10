"""CLI replay runner: the Cloud Run stand-in.

    python runner/regenerate.py --report-id <id> [--version N] [--as-of 2025-06-15]
                                [--out reports/] [--formats html,md]
    python runner/regenerate.py --list

Fetches a definition, validates its bindings against the knowledge graph, binds
the report-date token to --as-of, executes the named queries, runs the reasoning
steps over the fresh results, renders every requested format, and writes the
outputs. Each step is recorded as a local observability span.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runner import render  # noqa: E402
from server import knowledge_graph, reasoning, registry  # noqa: E402
from server.db import (  # noqa: E402
    ANCHOR_DATE,
    PROJECT_ROOT,
    get_connection,
    get_meta_connection,
)
from server.observability import RunRecorder  # noqa: E402
from server.parity import run_named_query  # noqa: E402

_EXTENSIONS = {"html": "html", "md": "md"}


def _make_stdout_printable() -> None:
    """Never let the console's codepage kill a replay.

    The narrative is free text -- an LLM engine happily returns an arrow or an
    em dash -- and a Windows console defaults to cp1252, which cannot encode
    them. The report file is always written as UTF-8; only the progress echo
    needs to degrade.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")


def _open_readonly():
    """Open the DuckDB warehouse read-only for a replay run.

    A read-only open takes a shared lock, so this succeeds while the MCP server
    is running -- the server holds the warehouse read-only too. The only thing
    that can refuse us is a read-write opener, which in this project is just
    ``data/seed.py``.
    """
    try:
        return get_connection(read_only=True)
    except duckdb.IOException as exc:
        raise SystemExit(
            "Cannot open the database read-only: another process holds it "
            "read-write. In this project only 'python data/seed.py' opens the "
            "warehouse read-write; wait for it to finish, then run this command "
            "again.\n"
            f"Underlying error: {exc}"
        ) from exc


def _list_reports(con) -> None:
    reports = registry.list_all(con)
    if not reports:
        print("No reports registered. Run the save flow first.")
        return
    print("Registered reports:")
    for r in reports:
        print(
            f"  {r['report_id']} v{r['definition_version']}  "
            f"'{r['report_name']}'  (parity attempts: {r['parity_attempts']})"
        )


_WATCH_OPS = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def _evaluate_watches(
    definition: dict, results_by_name: dict, recorder: RunRecorder
) -> dict[str, str]:
    """Flag editorial blocks whose premise no longer holds.

    A block replays verbatim -- that is the point -- so nothing else would notice
    when the numbers move out from under the prose. An unresolvable watch is
    itself a reason to flag the block, never a reason to crash the replay.
    """
    stale: dict[str, str] = {}
    for block in definition.get("editorial_blocks") or []:
        watch = block.get("watch")
        if not watch:
            continue
        block_id = block["block_id"]
        with recorder.span(f"watch:{block_id}") as span:
            try:
                result = results_by_name[watch["result"]]
                literal = render.selector_literal(watch["selector"])
                value = render.pick(result, literal, watch["field"])
                fired = _WATCH_OPS[watch["op"]](float(value), float(watch["value"]))
            except (KeyError, ValueError, TypeError, render.SelectorError) as exc:
                span.set(resolved=False)
                stale[block_id] = (
                    f"Authored {block['authored_as_of']}; watch condition "
                    f"'{watch['raw']}' can no longer be evaluated ({exc})."
                )
                continue
            span.set(resolved=True, fired=fired, value=value)
            if fired:
                stale[block_id] = (
                    f"Authored {block['authored_as_of']}; watch condition "
                    f"'{watch['raw']}' is now true."
                )
    return stale


def regenerate(
    report_id: str,
    version: int | None,
    as_of: str,
    out_dir: Path,
    formats: list[str] | None = None,
) -> list[Path]:
    con = _open_readonly()
    recorder = RunRecorder(report_id, version or 0, as_of)

    with recorder.span("fetch_definition"):
        definition = registry.get(get_meta_connection(), report_id, version)
    resolved_version = definition["definition_version"]
    recorder.version = resolved_version

    # Fetch definition, validate bindings.
    catalog = knowledge_graph.load_catalog(con)
    with recorder.span("validate_bindings") as span:
        errors = knowledge_graph.validate_bindings(
            catalog, definition.get("metric_bindings", [])
        )
        span.set(bindings=len(definition.get("metric_bindings", [])), errors=len(errors))
    if errors:
        for err in errors:
            print(f"  binding error: {err}")
        raise SystemExit(f"Binding validation failed for {report_id}")

    # Direct execution of each named query with the report date bound.
    results_by_name: dict[str, dict] = {}
    for query in definition["parameterized_sql"]:
        name = query["result_name"]
        with recorder.span(f"execute:{name}") as span:
            result = run_named_query(con, query["sql"], as_of)
            span.set(rows=len(result["rows"]))
        results_by_name[name] = result
        print(f"  query {name}: {len(result['rows'])} rows")

    # Run reasoning steps over the fresh results.
    with recorder.span("reasoning") as span:
        narratives = reasoning.get_engine().run(
            definition.get("reasoning_steps", []), results_by_name
        )
        span.set(steps=len(narratives))
    for step_id, text in narratives.items():
        print(f"  reasoning {step_id}: {text}")

    # Editorial prose replays verbatim, so it can go stale. Each watch condition
    # is re-evaluated against the fresh numbers; the ones that fire get a banner.
    stale_blocks = _evaluate_watches(definition, results_by_name, recorder)
    for block_id, message in stale_blocks.items():
        print(f"  editorial {block_id}: {message}")

    # Render and deliver every requested format.
    spec = definition["rendering_spec"]
    requested = formats or spec.get("formats", ["html"])
    if spec.get("layout") == render.TABBED_LAYOUT and [
        fmt for fmt in requested if fmt != "html"
    ]:
        for warning in definition.get("warnings", []):
            if "markdown output skipped" in warning:
                print(f"  {warning}")
        requested = [fmt for fmt in requested if fmt == "html"]

    out_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    for fmt in requested:
        if fmt not in _EXTENSIONS:
            print(f"  skipping unsupported format '{fmt}'")
            continue
        with recorder.span(f"render:{fmt}") as span:
            span.set(charts=len(spec.get("charts") or []))
            content = render.render(
                definition,
                results_by_name,
                narratives,
                fmt,
                as_of=as_of,
                stale_blocks=stale_blocks,
            )
        out_path = out_dir / f"{report_id}_v{resolved_version}_{as_of}.{_EXTENSIONS[fmt]}"
        out_path.write_text(content, encoding="utf-8")
        outputs.append(out_path)
        print(f"  wrote {out_path}")

    log_path = recorder.flush([str(p) for p in outputs])
    print(f"  spans: {len(recorder.spans)} recorded to {log_path}")
    return outputs


def main(argv: list[str] | None = None) -> None:
    _make_stdout_printable()
    parser = argparse.ArgumentParser(description="Regenerate a saved report.")
    parser.add_argument("--report-id")
    parser.add_argument("--version", type=int, default=None)
    parser.add_argument("--as-of", default=ANCHOR_DATE)
    parser.add_argument("--out", default=str(PROJECT_ROOT / "reports"))
    parser.add_argument(
        "--formats", default=None, help="Comma-separated override, e.g. html,md"
    )
    parser.add_argument("--list", action="store_true", help="List registered reports.")
    args = parser.parse_args(argv)

    if args.list:
        # Listing reads only the registry, so it never opens the warehouse.
        _list_reports(get_meta_connection())
        return
    if not args.report_id:
        parser.error("--report-id is required unless --list is given")
    formats = args.formats.split(",") if args.formats else None
    regenerate(args.report_id, args.version, args.as_of, Path(args.out), formats)


if __name__ == "__main__":
    main()
