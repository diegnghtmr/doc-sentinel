"""Deterministic aggregation and verdicts (PLAN §9). Pure functions; no LLM, no I/O."""

from __future__ import annotations

import copy
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from doc_sentinel.schema import SEVERITY_RANK, AdapterOutcome, Finding, Location

FAIL_ON = ("high", "medium")


@dataclass
class DetectVerdict:
    verdict: str  # "clean" | "findings" | "error"
    exit_code: int
    findings: list[Finding]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip().casefold()


def _overlap(a: list[float] | None, b: list[float] | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def _same(a: Finding, b: Finding) -> bool:
    return (a.cls == b.cls
            and a.location.channel == b.location.channel
            and a.location.page == b.location.page
            and _overlap(a.location.bbox, b.location.bbox)
            and _norm(a.excerpt) == _norm(b.excerpt))


def dedupe(findings: Iterable[Finding]) -> list[Finding]:
    """Merge findings with same class, channel, page, overlapping bbox and normalized excerpt.

    No voting: the merged finding keeps the maximum severity and lists every adapter.
    """
    merged: list[Finding] = []
    for item in findings:
        for kept in merged:
            if _same(kept, item):
                for name in item.adapters:
                    if name not in kept.adapters:
                        kept.adapter = kept.adapters + [name]
                if SEVERITY_RANK[item.severity] > SEVERITY_RANK[kept.severity]:
                    kept.severity = item.severity
                break
        else:
            clone = copy.deepcopy(item)
            clone.adapter = clone.adapters
            merged.append(clone)
    return merged


def _tool_error(outcome: AdapterOutcome) -> Finding:
    return Finding(adapter=[outcome.adapter], adapter_code=f"ADAPTER_{outcome.status.upper()}",
                   cls="tool_error", severity="info", confidence="declared",
                   location=Location(file="", channel="body"),
                   excerpt=outcome.reason or outcome.status)


def aggregate_detect(outcomes: list[AdapterOutcome], mandatory: set[str],
                     fail_on: str = "high") -> DetectVerdict:
    if fail_on not in FAIL_ON:
        raise ValueError(f"fail_on must be one of {FAIL_ON}")
    raw = [f for o in outcomes if o.status == "ok" for f in o.findings]
    findings = sorted(dedupe(raw), key=lambda f: -SEVERITY_RANK[f.severity])
    failed = [o for o in outcomes if o.status != "ok" and o.adapter in mandatory]
    findings += [_tool_error(o) for o in failed]
    for i, finding in enumerate(findings, 1):
        finding.finding_id = f"f-{i:03d}"

    substantive = [f for f in findings if f.cls != "tool_error"]
    top = max((SEVERITY_RANK[f.severity] for f in substantive), default=-1)
    threshold = SEVERITY_RANK[fail_on]

    if failed:
        # A mandatory layer did not run: never "clean". Keep "findings" only when what
        # did run already crosses the threshold (PLAN §18.3); otherwise "error".
        if top >= threshold:
            return DetectVerdict("findings", 1, findings)
        return DetectVerdict("error", 2, findings)
    if not substantive:
        return DetectVerdict("clean", 0, findings)
    return DetectVerdict("findings", 1 if top >= threshold else 0, findings)


def decide_clean(*, tool_failed: bool, residual: bool, changed: bool) -> tuple[str, int]:
    if tool_failed:
        return "error", 2
    if residual:
        return "residual", 1
    return ("cleaned", 0) if changed else ("already_clean", 0)
