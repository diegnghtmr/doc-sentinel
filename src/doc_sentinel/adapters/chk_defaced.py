"""chk_defaced is invoked only as an independent CLI process (AGPL)."""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from doc_sentinel.adapters.base import AdapterError, Capability, RunContext, repo_root, run_tool
from doc_sentinel.schema import Finding, Location


def _binary() -> str | None:
    candidate = repo_root() / "bin" / "chk_defaced"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return shutil.which("chk_defaced")


def _class(rule: str, category: str) -> str:
    if "GLYPH" in rule or "TOUNICODE" in rule or rule in {
        "FONT.PUA_CMAP", "PDF.CANONICAL_TAMPER_CONFIRMED", "DOCX.CANONICAL_TAMPER_CONFIRMED"
    }:
        return "tounicode_mismatch"
    if "RENDER_DIVERGENCE" in rule:
        return "visual_divergence"
    return {"HiddenContent": "hidden_text", "UnicodeHygiene": "unicode_smuggling",
            "FontIntegrity": "tounicode_mismatch", "Structural": "document_metadata"}.get(
                category, "document_metadata")


class ChkDefacedAdapter:
    id = "chk_defaced"
    mode = frozenset({"detect"})

    def available(self) -> Capability:
        return Capability(True) if _binary() else Capability(False, "chk_defaced binary not found")

    def inspect(self, src: Path, ctx: RunContext) -> list[Finding]:
        binary = _binary()
        if not binary:
            raise AdapterError("chk_defaced binary not found")
        cmd = [binary, "scan", str(src.resolve()), "--format-out", "json"]
        # The CLI's --registry takes an index path; do not pass it without an actual index.
        index = repo_root() / "bin" / "font-registry.json"
        if index.is_file():
            cmd.extend(["--registry", str(index)])
        proc = run_tool(cmd, ctx, ok_codes=(0, 1))
        try:
            reports = json.loads(proc.stdout)
            if not isinstance(reports, list) or len(reports) != 1:
                raise ValueError("expected one report")
            findings = []
            for item in reports[0]["findings"]:
                rule, category = item["rule"], item["category"]
                severity = item["severity"].lower()
                if severity == "high" and ("GLYPH_SEMANTIC_REPLACEMENT" in rule
                                           or "TOUNICODE_GARBLED" in rule):
                    severity = "critical"
                message = item["message"]
                example = re.search(r'e\.g\.\s+"(.*?)"', message, re.DOTALL)
                location = item.get("location", "")
                page = re.search(r"\bpage\s+(\d+)\b", location, re.IGNORECASE)
                findings.append(Finding(
                    adapter=self.id, adapter_code=rule, cls=_class(rule, category),
                    severity=severity, location=Location(file=src.name,
                        page=int(page.group(1)) if page else None, pointer=location or None),
                    excerpt=example.group(1) if example else "", confidence="heuristic",
                    evidence={"message": message, "category": category,
                              "confidence": item.get("confidence")}))
            return findings
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise AdapterError(f"invalid chk_defaced output: {exc}") from exc

    def clean(self, src: Path, dest: Path, ctx: RunContext):
        raise AdapterError("chk_defaced is detect-only")
