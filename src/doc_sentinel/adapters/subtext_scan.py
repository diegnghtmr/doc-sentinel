"""Adapter for the pinned subtext-scan CLI (PLAN §8)."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from doc_sentinel.adapters.base import AdapterError, Capability, RunContext, run_tool, third_party
from doc_sentinel.schema import Finding, Location

# Complete rule union in the pinned src/index.d.ts; classifications reflect the
# original channel, not a claim that hidden content is necessarily malicious.
_RULES = {
    "hidden-text": ("hidden_text", "body"),
    "ocr-text-layer": ("hidden_text", "body"),
    "instruction-phrase": ("prompt_injection", "body"),
    "metadata": ("metadata_injection", "metadata"),
    "annotation": ("metadata_injection", "annotation"),
    "attachment": ("embedded_file", "embedded"),
    "javascript": ("hidden_text", "annotation"),
    "comment": ("prompt_injection", "comment"),
    "tracked-change": ("prompt_injection", "tracked_change"),
    "html-comment": ("prompt_injection", "comment"),
    "html-attribute": ("link_or_alt_injection", "alt"),
    "unicode-tags": ("unicode_smuggling", "body"),
    "bidi-control": ("unicode_smuggling", "body"),
    "zero-width": ("unicode_smuggling", "body"),
}


def _classification(rule: str, fmt: str, loc: dict[str, Any]) -> tuple[str, str]:
    cls, channel = _RULES.get(rule, ("hidden_text", "body"))
    if rule == "hidden-text" and fmt == "html":
        channel = "html_hidden"
    if rule == "html-attribute":
        if loc.get("element") == "meta":
            cls, channel = "metadata_injection", "metadata"
        elif loc.get("element") == "input" and loc.get("attribute") == "value":
            cls, channel = "prompt_injection", "form_field"
    if loc.get("attachment"):
        channel = "embedded"
    return cls, channel


def _parse_report(raw: str, src: Path) -> list[Finding]:
    try:
        report = json.loads(raw)
        if "error" in report or not isinstance(report["findings"], list):
            raise ValueError("invalid findings")
        findings = []
        for item in report["findings"]:
            rule, severity = item["rule"], item["severity"]
            loc = item.get("location") or {}
            if (not isinstance(rule, str) or severity not in {"info", "low", "medium", "high"}
                    or not isinstance(loc, dict)):
                raise ValueError("invalid rule, severity, or location")
            cls, channel = _classification(rule, report.get("format", ""), loc)
            evidence = {"reason": item.get("reason", ""), "phrases": item.get("phrases", []),
                        "mechanisms": item.get("mechanisms", []),
                        "evidence": item.get("evidence", {}), "location": loc}
            if rule not in _RULES:
                evidence["mapping_note"] = "Unknown subtext-scan rule; mapped as hidden text."
            findings.append(Finding(
                adapter="subtext-scan", adapter_code=rule, cls=cls, severity=severity,
                confidence="heuristic", excerpt=item.get("text", ""), evidence=evidence,
                location=Location(file=src.name, page=item.get("page"), bbox=item.get("bbox"),
                                  channel=channel,
                                  pointer=json.dumps(loc, ensure_ascii=False, sort_keys=True)
                                  if loc else None),
            ))
        return findings
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise AdapterError("invalid subtext-scan JSON report") from exc


class SubtextScanAdapter:
    id = "subtext-scan"
    mode = frozenset({"detect"})

    def available(self) -> Capability:
        node = shutil.which("node")
        if not node:
            return Capability(False, "node executable not found")
        root = third_party("subtext-scan")
        if not (root / "bin" / "subtext-scan.mjs").is_file():
            return Capability(False, "subtext-scan CLI is missing")
        modules = root / "node_modules"
        if not modules.is_dir() or any(not (modules / dep).is_dir()
                                       for dep in ("fflate", "htmlparser2", "pdfjs-dist")):
            return Capability(False, "subtext-scan node_modules dependencies are missing")
        try:
            ctx = RunContext(workdir=Path(tempfile.gettempdir()), timeout_s=10)
            proc = run_tool([node, "--version"], ctx, ok_codes=(0,))
            match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)\s*", proc.stdout)
            if proc.returncode != 0 or not match:
                return Capability(False, "cannot determine node version")
            version = tuple(int(part) for part in match.groups())
            if version < (22, 13, 0):
                return Capability(False, "subtext-scan requires node >= 22.13")
        except AdapterError as exc:
            return Capability(False, f"cannot run node: {exc}")
        return Capability(True)

    def inspect(self, src: Path, ctx: RunContext) -> list[Finding]:
        node = shutil.which("node")
        if not node:
            raise AdapterError("node executable not found")
        script = third_party("subtext-scan") / "bin" / "subtext-scan.mjs"
        proc = run_tool([node, str(script), "scan", str(src.resolve()), "--json"], ctx,
                        ok_codes=(0, 1))
        if proc.returncode not in (0, 1):
            raise AdapterError(f"subtext-scan exited {proc.returncode}")
        return _parse_report(proc.stdout, src)

    def clean(self, src: Path, dest: Path, ctx: RunContext):
        raise NotImplementedError("subtext-scan is a detect-only adapter")
