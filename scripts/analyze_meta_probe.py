"""Summarize logs/meta_probe.jsonl to discover the shape of the client `_meta`.

Run the server with POC_LOG_META=1, drive a chat (or a few), then:

    python scripts/analyze_meta_probe.py

For every distinct `_meta` key seen, prints how many times it appeared, how many
distinct values it took, and the first/last timestamps -- so you can tell which
field is stable across a whole chat (few distinct values, wide time range) from
one that rotates per turn. This is how the correlation field is *discovered*, not
assumed; the default (`vscode.conversationId`) came from exactly this analysis.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.db import PROJECT_ROOT  # noqa: E402

PROBE_PATH = PROJECT_ROOT / "logs" / "meta_probe.jsonl"


def _load(path: Path) -> list[dict]:
    records: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def main() -> int:
    if not PROBE_PATH.exists():
        print(f"No probe log at {PROBE_PATH}.")
        print("Run the server with POC_LOG_META=1 and drive a chat first.")
        return 1

    records = _load(PROBE_PATH)
    if not records:
        print(f"{PROBE_PATH} is empty.")
        return 1

    print(f"{len(records)} probe records from {PROBE_PATH}\n")

    tools = defaultdict(int)
    with_meta = 0
    # key -> {value -> [count, first_ts, last_ts]}
    keys: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(lambda: [0, None, None]))
    explicit_seen = set()

    for record in records:
        tools[record.get("tool", "?")] += 1
        if record.get("explicit_conversation_id"):
            explicit_seen.add(record["explicit_conversation_id"])
        meta = record.get("meta") or {}
        if meta:
            with_meta += 1
        ts = record.get("ts")
        for key, value in meta.items():
            slot = keys[key][str(value)]
            slot[0] += 1
            slot[1] = slot[1] or ts
            slot[2] = ts

    print("Calls by tool:")
    for tool, count in sorted(tools.items()):
        print(f"  {tool:<24} {count}")
    print(f"\nRecords carrying any _meta: {with_meta}/{len(records)}")
    if explicit_seen:
        print(f"Explicit conversation_ids also seen: {len(explicit_seen)}")

    if not keys:
        print("\nNo _meta keys were ever present. The client sends no _meta, or it is "
              "not reaching the server. Correlation will fall back to generated keys.")
        return 0

    print("\n_meta fields (a chat-stable id has FEW distinct values over a WIDE time range):")
    for key in sorted(keys):
        values = keys[key]
        total = sum(v[0] for v in values.values())
        distinct = len(values)
        first = min((v[1] for v in values.values() if v[1]), default="?")
        last = max((v[2] for v in values.values() if v[2]), default="?")
        print(f"\n  {key}")
        print(f"    {total} occurrences, {distinct} distinct value(s), {first} .. {last}")
        for value, (count, vfirst, vlast) in sorted(values.items(), key=lambda kv: -kv[1][0])[:8]:
            shown = value if len(value) <= 44 else value[:41] + "..."
            print(f"      {count:>4}x  {shown}   ({vfirst} .. {vlast})")
        if distinct > 8:
            print(f"      ... and {distinct - 8} more distinct values")

    print("\nA good correlation key: 1 distinct value within one chat, a new value in a new "
          "chat. Set it via POC_CORRELATION_META_KEYS if it is not vscode.conversationId.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
