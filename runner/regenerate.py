"""CLI replay runner: the Cloud Run stand-in.

    python runner/regenerate.py --report-id <id> [--version N] [--as-of 2025-06-15]
                                [--out reports/]
    python runner/regenerate.py --list

Fetches a definition, binds the report-date token to --as-of, executes the named
queries, renders the template, and writes an HTML file.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runner import render  # noqa: E402
from server import registry  # noqa: E402
from server.db import ANCHOR_DATE, PROJECT_ROOT, get_connection  # noqa: E402
from server.parity import run_named_query  # noqa: E402


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
) -> Path:
    con = get_connection(read_only=True)
    definition = registry.get(con, report_id, version)
    resolved_version = definition["definition_version"]

    results_by_name = {}
    for query in definition["queries"]:
        name = query["result_name"]
        result = run_named_query(con, query["sql"], as_of)
        results_by_name[name] = result
        print(f"  query {name}: {len(result['rows'])} rows")

    html = render.render_definition(definition, results_by_name)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{report_id}_v{resolved_version}_{as_of}.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"Wrote {out_path}")
    return out_path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Regenerate a saved report.")
    parser.add_argument("--report-id")
    parser.add_argument("--version", type=int, default=None)
    parser.add_argument("--as-of", default=ANCHOR_DATE)
    parser.add_argument("--out", default=str(PROJECT_ROOT / "reports"))
    parser.add_argument("--list", action="store_true", help="List registered reports.")
    args = parser.parse_args(argv)

    con = get_connection(read_only=True)
    if args.list:
        _list_reports(con)
        return
    if not args.report_id:
        parser.error("--report-id is required unless --list is given")
    regenerate(args.report_id, args.version, args.as_of, Path(args.out))


if __name__ == "__main__":
    main()
