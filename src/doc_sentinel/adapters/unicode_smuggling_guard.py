"""Scan extracted channels in one isolated Python child using the pinned library API.

The CLI has no JSON format and loses channel provenance, so the runner emits JSON
from the library's scanner and decoder rather than parsing its human-facing output.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from doc_sentinel.adapters.base import AdapterError, Capability, RunContext, run_tool
from doc_sentinel.schema import Finding, Location

_RUNNER = """
import json, sys
from unicode_smuggling_guard.scanner import scan
from unicode_smuggling_guard.decode import decode
channels = json.load(sys.stdin)
results = []
for index, text in enumerate(channels):
    for item in scan(text):
        results.append(dict(index=index, category=item.category.value,
                            codepoints=item.codepoints, decoded=decode(item.category, item.codepoints),
                            line=item.line, column=item.column))
json.dump(results, sys.stdout, ensure_ascii=True)
"""


class UnicodeSmugglingGuardAdapter:
    id = "unicode-smuggling-guard"
    mode = frozenset({"detect"})

    def available(self) -> Capability:
        if importlib.util.find_spec("unicode_smuggling_guard") is None:
            return Capability(False, "unicode-smuggling-guard Python library not installed")
        return Capability(True)

    def inspect(self, src: Path, ctx: RunContext) -> list[Finding]:
        if not ctx.channels:
            return []
        proc = run_tool([sys.executable, "-c", _RUNNER], ctx,
                        stdin=json.dumps([c.text for c in ctx.channels]), ok_codes=(0,))
        try:
            rows = json.loads(proc.stdout)
            if not isinstance(rows, list):
                raise TypeError("expected a list")
            out = []
            for row in rows:
                index = row["index"]
                if not isinstance(index, int) or not 0 <= index < len(ctx.channels):
                    raise ValueError("invalid channel index")
                channel, category = ctx.channels[index], row["category"]
                decoded = row.get("decoded")
                severity = {"tag": "critical" if decoded else "high", "bidi": "high",
                            "zero-width": "medium", "variation-selector": "medium",
                            "control": "medium", "invisible": "medium",
                            "control-token": "high"}[category]
                out.append(Finding(
                    adapter=self.id, adapter_code=category,
                    cls="filename_injection" if channel.channel == "filename"
                        else "unicode_smuggling", severity=severity,
                    location=Location(file=src.name, channel=channel.channel,
                                      pointer=channel.pointer, page=channel.page),
                    excerpt=channel.text, confidence="measured",
                    evidence={"codepoints": row["codepoints"], "decoded": decoded,
                              "line": row["line"], "column": row["column"],
                              "hidden": channel.hidden}))
            return out
        except (ValueError, TypeError, KeyError) as exc:
            raise AdapterError(f"invalid unicode-smuggling-guard output: {exc}") from exc

    def clean(self, src: Path, dest: Path, ctx: RunContext):
        raise AdapterError("unicode-smuggling-guard is detect-only")
