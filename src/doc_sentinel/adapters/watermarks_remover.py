"""Deterministic cleaning through the pinned watermarks-remover file CLIs."""
from __future__ import annotations

import filecmp
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from doc_sentinel.adapters.base import (
    AdapterError,
    Capability,
    CleanResult,
    RunContext,
    run_tool,
    third_party,
)
from doc_sentinel.schema import Finding, Location


class WatermarksRemoverAdapter:
    id = "watermarks-remover"
    mode = frozenset({"clean"})

    @staticmethod
    def _script(name: str) -> Path:
        return third_party("watermarks-remover") / "service" / "scripts" / name

    def available(self) -> Capability:
        for name in ("inspect_file.py", "clean_file.py"):
            if not self._script(name).is_file():
                return Capability(False, f"missing watermarks-remover script: {self._script(name)}")
        if not Path(sys.executable).is_file():
            return Capability(False, "Python interpreter is unavailable")
        return Capability(True)

    @staticmethod
    def _json(stdout: str) -> dict[str, Any]:
        try:
            value = json.loads(stdout)
        except (ValueError, TypeError) as exc:
            raise AdapterError("watermarks-remover returned invalid JSON") from exc
        if not isinstance(value, dict) or value.get("kind") not in {
            "text", "image", "av", "container"
        }:
            raise AdapterError("watermarks-remover returned an unsupported or invalid result")
        return value

    def _inspect(self, src: Path, ctx: RunContext) -> dict[str, Any]:
        proc = run_tool([sys.executable, str(self._script("inspect_file.py")), "--json", str(src)],
                        ctx, ok_codes=(0, 1))
        value = self._json(proc.stdout)
        if value["kind"] == "text":
            if not isinstance(value.get("hits"), list) or "suspicious_total" not in value:
                raise AdapterError("incomplete text inspection")
        elif not isinstance(value.get("has_c2pa"), bool) or not isinstance(
                value.get("has_ai_metadata"), bool):
            raise AdapterError("incomplete metadata inspection")
        return value

    def inspect(self, src: Path, ctx: RunContext) -> list[Finding]:
        report = self._inspect(src, ctx)
        findings: list[Finding] = []
        if report.get("has_c2pa"):
            findings.append(Finding(adapter=self.id, adapter_code="C2PA", cls="c2pa_manifest",
                                    severity="info", confidence="measured",
                                    location=Location(file=str(src), channel="metadata")))
        if report.get("has_ai_metadata"):
            findings.append(Finding(adapter=self.id, adapter_code="AI_METADATA",
                                    cls="document_metadata", severity="info",
                                    confidence="heuristic",
                                    location=Location(file=str(src), channel="metadata")))
        for hit in report.get("layer_a_hits", report.get("hits", [])):
            if not isinstance(hit, dict):
                raise AdapterError("invalid Unicode inspection hit")
            findings.append(Finding(adapter=self.id, adapter_code="UNICODE",
                                    cls="unicode_smuggling", severity="info",
                                    confidence="measured",
                                    location=Location(file=str(src), channel="body"),
                                    evidence={k: hit[k] for k in ("codepoint", "count", "kind")
                                              if k in hit}))
        return findings

    def clean(self, src: Path, dest: Path, ctx: RunContext) -> CleanResult:
        src, dest = Path(src), Path(dest)
        if src.resolve() == dest.resolve() or (dest.exists() and src.samefile(dest)):
            raise ValueError("output path must differ from input")
        before = self._inspect(src, ctx)
        # These are PDF-only controls in the upstream CLI. Its default PDF image
        # pass is conditional and its attachment pass is always enabled. OOXML
        # embedded media is independently scrubbed by the normal container path.
        proc = run_tool([sys.executable, str(self._script("clean_file.py")), str(src),
                         "-o", str(dest), "--json"], ctx, ok_codes=(0, 1))
        cleaned = self._json(proc.stdout)
        if not dest.is_file():
            raise AdapterError("watermarks-remover produced no output file")
        after = self._inspect(dest, ctx)
        c2_before = bool(before.get("has_c2pa", False))
        c2_after = bool(after.get("has_c2pa", False))
        actions: list[dict[str, Any]] = []
        stats = cleaned.get("stats", {})
        if isinstance(stats, dict):
            for action in ("removed", "replaced"):
                count = stats.get(f"{action}_count", 0)
                if count:
                    actions.append({"action": action, "what": "Unicode characters", "count": count,
                                    "details": stats.get(action, {})})
        for native in cleaned.get("actions", []):
            if not isinstance(native, str) or native.lower().startswith("no "):
                continue
            layer = re.search(r"layer A text: removed=(\d+) replaced=(\d+)", native, re.IGNORECASE)
            if layer:
                for action, count in zip(("removed", "replaced"), layer.groups()):
                    if int(count):
                        actions.append({"action": action, "what": "Unicode characters",
                                        "count": int(count)})
                continue
            count_match = re.search(r"\bx(\d+)\b", native)
            record: dict[str, Any] = {"action": "cleaned" if native.startswith("clean ")
                                      else "removed", "what": native,
                                      "count": int(count_match.group(1)) if count_match else 1}
            if "c2pa" in native.casefold():
                record["c2pa"] = True
            # Native media actions do not always say C2PA. Match the part to the
            # pre-inspection evidence so partial removal is not silently omitted.
            for finding in before.get("findings", []):
                if isinstance(finding, str) and "c2pa" in finding.casefold():
                    part = finding.split(":", 1)[0]
                    if "/" in part and part in native:
                        record["c2pa"] = True
            actions.append(record)
        if c2_before and not c2_after:
            actions.append({"action": "removed", "what": "C2PA detected before cleaning",
                            "count": 1, "c2pa": True})
        if c2_after:
            actions.append({"action": "residual", "what": "C2PA detected after cleaning",
                            "count": 1, "c2pa": True})
        hits = after.get("layer_a_hits", after.get("hits", []))
        residual = bool(c2_after or after.get("has_ai_metadata") or hits
                        or after.get("suspicious_total", 0))
        warnings = []
        if shutil.which("exiftool") is None:
            warnings.append("exiftool unavailable: metadata inspection and PDF removal are less reliable")
        if shutil.which("c2patool") is None:
            warnings.append("c2patool unavailable: C2PA inspection cannot verify all manifests")
        meta = cleaned.get("meta", {})
        if isinstance(meta, dict) and meta.get("degraded"):
            warnings.append("watermarks-remover reported degraded, best-effort cleaning")
        return CleanResult(changed=not filecmp.cmp(src, dest, shallow=False), actions=actions,
                           c2pa_before=c2_before, c2pa_after=c2_after, residual=residual,
                           warnings=warnings)
