"""Lightweight, cloud-free observability.

The design emits OTel spans to Cloud Trace. This POC records the same shape of
information (named spans with durations and attributes) to a local JSONL file so
the runner stays lock-free and needs no exporter. Each replay writes one run
record holding its ordered spans.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from server.db import PROJECT_ROOT

_LOG_PATH = PROJECT_ROOT / "logs" / "spans.jsonl"


def log_span(name: str, **attributes: object) -> None:
    """Append one standalone span line to the span log.

    A lighter sibling of ``RunRecorder`` for the request boundary, where there is
    no multi-step run to record -- just a single event and its attributes (e.g. a
    tool call's resolved correlation source). Best-effort: never let diagnostics
    break a tool call, so a write failure is swallowed.
    """
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"span": name, "attributes": attributes}) + "\n")
    except OSError:
        pass


class RunRecorder:
    """Collect ordered spans for a single replay run."""

    def __init__(self, report_id: str, version: int, as_of: str) -> None:
        self.report_id = report_id
        self.version = version
        self.as_of = as_of
        self.spans: list[dict] = []

    def span(self, name: str, **attributes: object) -> "_Span":
        return _Span(self, name, attributes)

    def _record(self, name: str, duration_ms: float, attributes: dict) -> None:
        self.spans.append(
            {"name": name, "duration_ms": round(duration_ms, 2), "attributes": attributes}
        )

    def flush(self, output_paths: list[str]) -> Path:
        """Append this run's record to the local span log and return its path."""
        record = {
            "report_id": self.report_id,
            "definition_version": self.version,
            "as_of": self.as_of,
            "spans": self.spans,
            "outputs": output_paths,
        }
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        return _LOG_PATH


class _Span:
    """Context manager timing a single span."""

    def __init__(self, recorder: RunRecorder, name: str, attributes: dict) -> None:
        self._recorder = recorder
        self._name = name
        self._attributes = attributes
        self._start = 0.0

    def __enter__(self) -> "_Span":
        self._start = time.perf_counter()
        return self

    def set(self, **attributes: object) -> None:
        self._attributes.update(attributes)

    def __exit__(self, *_exc: object) -> None:
        duration_ms = (time.perf_counter() - self._start) * 1000.0
        self._recorder._record(self._name, duration_ms, self._attributes)
