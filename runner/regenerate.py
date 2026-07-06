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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runner import render  # noqa: E402
from server import knowledge_graph, reasoning, registry  # noqa: E402
from server.db import ANCHOR_DATE, PROJECT_ROOT, get_connection  # noqa: E402
from server.observability import RunRecorder  # noqa: E402
from server.parity import run_named_query  # noqa: E402

_EXTENSIONS = {"html": "html", "md": "md"}


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


def regenerate(
    report_id: str,
    version: int | None,
    as_of: str,
    out_dir: Path,
    formats: list[str] | None = None,
) -> list[Path]:
    con = get_connection(read_only=True)
    recorder = RunRecorder(report_id, version or 0, as_of)

    with recorder.span("fetch_definition"):
        definition = registry.get(con, report_id, version)
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

    # Render and deliver every requested format.
    requested = formats or definition["rendering_spec"].get("formats", ["html"])
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    for fmt in requested:
        if fmt not in _EXTENSIONS:
            print(f"  skipping unsupported format '{fmt}'")
            continue
        with recorder.span(f"render:{fmt}"):
            content = render.render(definition, results_by_name, narratives, fmt)
        out_path = out_dir / f"{report_id}_v{resolved_version}_{as_of}.{_EXTENSIONS[fmt]}"
        out_path.write_text(content, encoding="utf-8")
        outputs.append(out_path)
        print(f"  wrote {out_path}")

    log_path = recorder.flush([str(p) for p in outputs])
    print(f"  spans: {len(recorder.spans)} recorded to {log_path}")
    return outputs


def main(argv: list[str] | None = None) -> None:
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

    con = get_connection(read_only=True)
    if args.list:
        _list_reports(con)
        return
    if not args.report_id:
        parser.error("--report-id is required unless --list is given")
    formats = args.formats.split(",") if args.formats else None
    regenerate(args.report_id, args.version, args.as_of, Path(args.out), formats)


if __name__ == "__main__":
    main()
