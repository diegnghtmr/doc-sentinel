"""Allow-listed JSON for an orchestrator that must not see document content.

Only paths supplied as CLI arguments (or the clean output derived from the input
argument), controlled vocabularies, counts, and generated review identifiers are
emitted. Reasons, warnings, excerpts, evidence, and report-provided paths are not.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from doc_sentinel.registry import ADAPTERS
from doc_sentinel.schema import CHANNELS, CLASSES, SEVERITIES

_ADAPTERS = frozenset(ADAPTERS) | {"channel-extract", "agent-review"}
_BATCH_ID = re.compile(r"batch-[0-9]{3}\Z")
_ERROR_STATUS = frozenset({"missing", "invalid", "oversize", "unreadable", "unexpected"})


def _known_counts(values: list[Any], vocabulary: frozenset[str] | set[str]) -> dict[str, int]:
    counts = Counter(value for value in values if isinstance(value, str) and value in vocabulary)
    return dict(sorted(counts.items()))


def _known_adapters(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    result = []
    for item in items:
        value = item.get("id") if isinstance(item, dict) else item
        if isinstance(value, str) and value in _ADAPTERS and value not in result:
            result.append(value)
    return result


def make_summary(report: dict[str, Any], *, mode: str, exit_code: int,
                 input_path: str | None, output_path: str | None,
                 report_path: str | None, review: dict[str, Any] | None = None) -> dict[str, Any]:
    findings = report.get("findings", [])
    findings = [item for item in findings if isinstance(item, dict)] if isinstance(findings, list) else []
    adapters = []
    for finding in findings:
        value = finding.get("adapter", [])
        adapters.extend(value if isinstance(value, list) else [value])
    channels = [item.get("location", {}).get("channel")
                if isinstance(item.get("location"), dict) else None for item in findings]
    errors = []
    for error in report.get("review_errors", []):
        if not isinstance(error, dict) or error.get("status") not in _ERROR_STATUS:
            continue
        batch = error.get("batch_id")
        errors.append({"batch_id": batch if isinstance(batch, str) and _BATCH_ID.fullmatch(batch)
                       else None, "status": error["status"]})
    verdict = report.get("verdict")
    if verdict not in {"clean", "findings", "error", "cleaned", "already_clean", "residual"}:
        verdict = "error"
    return {
        "schema_version": 1,
        "mode": mode,
        "verdict": verdict,
        "exit_code": exit_code,
        "counts": {
            "severity": _known_counts([item.get("severity") for item in findings], set(SEVERITIES)),
            "class": _known_counts([item.get("class") for item in findings], CLASSES),
            "channel": _known_counts(channels, CHANNELS),
            "adapter": _known_counts(adapters, _ADAPTERS),
        },
        "adapters": {
            "run": _known_adapters(report.get("adapters_run")),
            "skipped": _known_adapters(report.get("adapters_skipped")),
            "failed": _known_adapters(report.get("adapters_failed")),
        },
        "input_path": input_path,
        "output_path": output_path,
        "report_path": report_path,
        "review": review,
        "review_errors": errors,
    }
