"""Adapter for hidden-text-detector's PDF and DOCX CLI.

Native CRITICAL is mapped to high, not critical: a measured concealment alone
is not proof of a critical injection (PLAN §8 reserves critical for stronger
signals). WARNING maps to medium and INFO to info.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from pathlib import Path

from doc_sentinel.adapters.base import AdapterError, Capability, RunContext, run_tool, third_party
from doc_sentinel.schema import Finding, Location

_SEVERITY = {"CRITICAL": "high", "WARNING": "medium", "INFO": "info"}
_PAGE = re.compile(r"\bpage\s+(\d+)\b", re.IGNORECASE)
_CONTENT = re.compile(r"\bcontent:\s*", re.IGNORECASE)


def _excerpt(detail: str) -> str:
    # The upstream script appends Python repr() of the document content.
    match = _CONTENT.search(detail)
    if not match:
        return ""
    try:
        value = ast.literal_eval(detail[match.end():].strip())
    except (ValueError, SyntaxError):
        return ""
    return value if isinstance(value, str) else ""


def _parse_report(raw: str, src: Path) -> list[Finding]:
    try:
        report = json.loads(raw)
        rows = report[str(src.resolve())]
        if not isinstance(rows, list):
            raise TypeError("invalid findings")
        findings = []
        for item in rows:
            where, detail = item["where"], item["detail"]
            if not isinstance(where, str) or not isinstance(detail, str):
                raise TypeError("invalid location or detail")
            match = _PAGE.search(where)
            findings.append(Finding(
                adapter="hidden-text-detector", adapter_code=item["type"], cls="hidden_text",
                severity=_SEVERITY[item["severity"]], confidence="heuristic",
                location=Location(file=src.name, page=int(match.group(1)) if match else None,
                                  pointer=where or None),
                excerpt=_excerpt(detail), evidence={"detail": detail, "where": where},
            ))
        return findings
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise AdapterError("invalid hidden-text-detector JSON report") from exc


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


class HiddenTextDetectorAdapter:
    id = "hidden-text-detector"
    mode = frozenset({"detect"})

    def available(self) -> Capability:
        script = third_party("hidden-text-detector") / "scripts" / "scan.py"
        if not script.is_file():
            return Capability(False, "hidden-text-detector scan.py is missing")
        missing = [name for name in ("fitz", "docx") if not _has_module(name)]
        if missing:
            return Capability(False, "missing Python modules: " + ", ".join(missing))
        return Capability(True, features={"ocg": _has_module("pikepdf"),
                                          "numpy": _has_module("numpy")})

    def inspect(self, src: Path, ctx: RunContext) -> list[Finding]:
        script = third_party("hidden-text-detector") / "scripts" / "scan.py"
        proc = run_tool([sys.executable, str(script), "--json", str(src.resolve())], ctx,
                        ok_codes=(0, 1))
        if proc.returncode not in (0, 1):
            raise AdapterError(f"hidden-text-detector exited {proc.returncode}")
        return _parse_report(proc.stdout, src)

    def clean(self, src: Path, dest: Path, ctx: RunContext):
        raise NotImplementedError("hidden-text-detector is a detect-only adapter")
