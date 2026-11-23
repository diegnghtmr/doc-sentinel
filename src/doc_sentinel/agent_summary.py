"""Allow-listed JSON for an orchestrator that must not see document content.

Only paths supplied as CLI arguments (or the clean output derived from the input
argument), controlled vocabularies, counts, page numbers, generated review identifiers
and fixed explanatory strings written here are emitted. Reasons, warnings, excerpts,
evidence, pointers, and report-provided paths are not.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from doc_sentinel.registry import ADAPTERS
from doc_sentinel.review import HIDDEN_CLASSES
from doc_sentinel.schema import CHANNELS, CLASSES, SEVERITIES, SEVERITY_RANK

_ADAPTERS = frozenset(ADAPTERS) | {"channel-extract", "agent-review"}
_BATCH_ID = re.compile(r"batch-[0-9]{3}\Z")
_ERROR_STATUS = frozenset({"missing", "invalid", "oversize", "unreadable", "unexpected"})
_TECHNIQUES = frozenset({"hidden-run", "tracked_deletion", "tracked_insertion_accepted",
                         "invisible-unicode", "visible-injection", "hidden-element", "html-comment",
                         "render-mode-3", "optional-content-off", "zero-opacity", "outside-cropbox",
                         "tiny-background-match", "hidden-region", "hyperlink-injection",
                         "metadata-injection", "filename-injection"})
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


_MAX_PAGES = 20
_MAX_PAGE_NUMBER = 99_999  # a larger integer could smuggle encoded data

# Fixed plain-language meaning of each finding class, for the orchestrator to relay
# (translated) to a non-expert user. Never derived from the document.
EXPLANATIONS = {
    "hidden_text": "Text that is in the file but invisible to a person (for example white, tiny "
                   "or hidden-formatted text). An AI that reads the file still sees it.",
    "unicode_smuggling": "Invisible Unicode characters that can hide or alter instructions for an "
                         "AI without changing what a person sees.",
    "visual_divergence": "What the document shows on screen differs from the text a machine "
                         "extracts from it.",
    "tounicode_mismatch": "The PDF font mapping makes the extracted text differ from the drawn "
                          "letters, so a machine reads something other than what is displayed.",
    "prompt_injection": "Instructions aimed at an AI assistant (for example to ignore its rules or "
                        "to grade or answer in a particular way) instead of normal content for a "
                        "human reader.",
    "metadata_injection": "AI-directed instructions placed in the document properties (title, "
                          "author, subject and similar fields).",
    "filename_injection": "The file name itself contains AI-directed instructions.",
    "link_or_alt_injection": "AI-directed instructions placed in links or in image alternative text.",
    "embedded_file": "The document contains embedded files that are not shown as normal content.",
    "c2pa_manifest": "The file carries a C2PA provenance manifest (for example an AI-generation "
                     "or editing record).",
    "document_metadata": "Document metadata worth knowing about, such as the authoring tool or "
                         "AI-generation markers.",
    "tool_error": "One of the detectors failed on this file, so the result may be incomplete.",
}

_NEXT_STEPS = {
    "sanitized_copy": "Create a separate copy without the hidden or injected content (the user can "
                      "say, e.g., 'déjalo limpio' / 'give me a clean copy'). The original is never "
                      "modified.",
    "clean_metadata": "Create a separate copy without metadata, provenance marks (C2PA) and "
                      "invisible Unicode (the user can say, e.g., 'límpialo' / 'clean this').",
}


def _page(location: Any) -> int | None:
    page = location.get("page") if isinstance(location, dict) else None
    return page if type(page) is int and 0 < page <= _MAX_PAGE_NUMBER else None


def _overview(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group findings by kind so the user can see what was found and where, without text."""
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for finding in findings:
        cls, severity = finding.get("class"), finding.get("severity")
        location = finding.get("location")
        channel = location.get("channel") if isinstance(location, dict) else None
        if not all(isinstance(value, str) for value in (cls, severity, channel)) or (
                cls not in CLASSES or severity not in SEVERITY_RANK or channel not in CHANNELS):
            continue
        evidence = finding.get("evidence")
        # "hidden" = not shown as normal page text, the rule review.py uses for severity.
        hidden = cls in HIDDEN_CLASSES or channel != "body" or (
            isinstance(evidence, dict) and evidence.get("hidden") is True)
        key = (cls, severity, "hidden" if hidden else "visible", channel)
        group = groups.setdefault(key, {"class": cls, "severity": severity,
                                        "visibility": key[2], "channel": channel,
                                        "count": 0, "pages": set()})
        group["count"] += 1
        page = _page(location)
        if page is not None:
            group["pages"].add(page)
    result = []
    for group in groups.values():
        group["pages"] = sorted(group["pages"])[:_MAX_PAGES]
        result.append(group)
    return sorted(result, key=lambda g: (-SEVERITY_RANK[g["severity"]], g["visibility"] != "hidden",
                                         g["class"], g["channel"]))


def _next_steps(mode: str, verdict: str, has_sanitize: bool) -> list[dict[str, str]]:
    if has_sanitize or mode not in {"detect", "review-merge"}:
        ids: list[str] = []
    elif verdict == "findings":
        ids = ["sanitized_copy", "clean_metadata"]
    elif verdict == "clean":
        ids = ["clean_metadata"]
    else:
        ids = []
    return [{"id": step, "description": _NEXT_STEPS[step]} for step in ids]


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
            reason in {"detected hidden content cannot be located",
                       "reviewed hidden content cannot be located",
                       "reviewed injection has no exact editable sentence",
                       "reviewed injection has no exact editable source span",
                       "output name reuses the injection-bearing input filename",
                       "rendered visible text changed", "rendered visual similarity below 0.97",
                       "page count changed"} or
            reason in _PDF_LOCATION_REFUSALS):
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
        "overview": _overview(findings),
        "explanations": {cls: EXPLANATIONS[cls] for cls in sorted(
            {item.get("class") for item in findings if isinstance(item.get("class"), str)}
            & CLASSES)},
        "next_steps": _next_steps(mode, verdict, isinstance(report.get("sanitize"), dict)),
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
        render = checks.get("render_equal") if isinstance(checks, dict) else None
        if (isinstance(render, dict) and render.get("status") == "passed" and
                render.get("mode") in {"exact", "equivalent_with_reflow"}):
            summary["sanitize"]["render_mode"] = render["mode"]
        scores = checks.get("visual_similarity") if isinstance(checks, dict) else None
        if (isinstance(scores, list) and scores and all(
                type(value) in (int, float) and math.isfinite(value) and -1 <= value <= 1
                for value in scores)):
            summary["sanitize"]["visual_similarity"] = {"min": min(scores), "mean": sum(scores) / len(scores)}
    return summary
