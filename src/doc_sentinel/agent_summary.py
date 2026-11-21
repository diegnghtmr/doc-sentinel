"""Allow-listed JSON for an orchestrator that must not see document content.

Only paths supplied as CLI arguments (or the clean output derived from the input
argument), controlled vocabularies, counts, and generated review identifiers are
emitted. Reasons, warnings, excerpts, evidence, and report-provided paths are not.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from doc_sentinel.registry import ADAPTERS
from doc_sentinel.schema import CHANNELS, CLASSES, SEVERITIES

_ADAPTERS = frozenset(ADAPTERS) | {"channel-extract", "agent-review"}
_BATCH_ID = re.compile(r"batch-[0-9]{3}\Z")
_ERROR_STATUS = frozenset({"missing", "invalid", "oversize", "unreadable", "unexpected"})
_TECHNIQUES = frozenset({"hidden-run", "tracked_deletion", "tracked_insertion_accepted",
                         "invisible-unicode", "visible-injection", "hidden-element", "html-comment",
                         "render-mode-3", "optional-content-off", "zero-opacity", "outside-cropbox",
                         "tiny-background-match", "hidden-region", "hyperlink-injection",
                         "metadata-injection"})
_SANITIZE_STATUS = frozenset({"sanitized", "nothing_to_do", "refused", "unsupported"})
_PDF_LOCATION_REFUSALS = frozenset({
    "PDF hidden finding has an invalid page",
    "PDF hidden finding has no complete native text",
    "PDF hidden text geometry cannot be verified",
    "PDF hidden text match is missing or ambiguous",
    "PDF hidden text match is not verifiably hidden",
    "PDF aggregate criterion has an unverifiable span",
    "PDF aggregate sample is not covered by excluded text",
})


def _safe_refusal(status: str, reason: Any) -> str | None:
    """Never forward an exception, document-derived text or arbitrary report string."""
    if status not in {"refused", "unsupported"}:
        return None
    if status == "unsupported":
        return "sanitize unsupported for this format"
    if isinstance(reason, str) and reason.startswith("glyph/ToUnicode divergence"):
        return "glyph/ToUnicode divergence lacks unambiguous mapping provenance"
    if isinstance(reason, str) and reason.startswith("original input missing or changed"):
        return "original input missing or changed since detection"
    if isinstance(reason, str) and (
            reason == "detected hidden content cannot be located" or reason in _PDF_LOCATION_REFUSALS):
        return reason
    return "sanitization could not be completed safely"


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
    summary = {
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
    sanitize = report.get("sanitize")
    if isinstance(sanitize, dict):
        status = sanitize.get("status")
        if status not in _SANITIZE_STATUS:
            status = "refused"
        actions = sanitize.get("actions") if isinstance(sanitize.get("actions"), list) else []
        actions = [item for item in actions if isinstance(item, dict)]
        summary["sanitize"] = {
            "status": status,
            "output_path": output_path if status == "sanitized" else None,
            "refusal_reason": _safe_refusal(status, sanitize.get("refusal_reason")),
            "counts": {
                "technique": _known_counts([a.get("technique") for a in actions], _TECHNIQUES),
                "visibility": _known_counts([a.get("visibility") for a in actions], {"visible", "hidden"}),
            },
        }
        checks = sanitize.get("checks")
        scores = checks.get("visual_similarity") if isinstance(checks, dict) else None
        if (isinstance(scores, list) and scores and all(
                type(value) in (int, float) and math.isfinite(value) and -1 <= value <= 1
                for value in scores)):
            summary["sanitize"]["visual_similarity"] = {"min": min(scores), "mean": sum(scores) / len(scores)}
    return summary
