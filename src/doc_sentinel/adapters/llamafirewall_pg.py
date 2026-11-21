"""Offline Prompt Guard 2 adapter; model execution is confined to a child process."""
from __future__ import annotations

import importlib.util
import json
import math
import os
import sys
from pathlib import Path

from doc_sentinel.adapters.base import AdapterError, Capability, RunContext, run_tool
from doc_sentinel.schema import Finding, Location

MODEL = "meta-llama/Llama-Prompt-Guard-2-86M"
_MISSING = ("Prompt Guard 2 model not in local Hugging Face cache; run hf auth login then "
            "hf download meta-llama/Llama-Prompt-Guard-2-86M")


def _local_snapshot() -> Path | None:
    """Find a complete cached snapshot without importing the hub or contacting it."""
    if os.environ.get("HF_HUB_CACHE"):
        hub = Path(os.environ["HF_HUB_CACHE"]).expanduser()
    else:
        home = os.environ.get("HF_HOME")
        cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        hub = (Path(home).expanduser() if home else cache / "huggingface") / "hub"
    base = hub / "models--meta-llama--Llama-Prompt-Guard-2-86M"
    refs = base / "refs" / "main"
    snapshots = base / "snapshots"
    candidates = []
    if refs.is_file():
        revision = refs.read_text(encoding="utf-8").strip()
        if revision and revision.isalnum():
            candidates.append(snapshots / revision)
    if snapshots.is_dir():
        candidates.extend(sorted(snapshots.iterdir(), reverse=True))
    for path in candidates:
        if (path.is_dir() and (path / "config.json").is_file()
                and any((path / name).is_file() for name in
                        ("model.safetensors", "pytorch_model.bin", "model.safetensors.index.json"))
                and any((path / name).is_file() for name in
                        ("tokenizer.json", "tokenizer.model", "vocab.json"))):
            return path
    return None


class PromptGuardAdapter:
    id = "prompt-guard"
    mode = frozenset({"detect"})

    def available(self) -> Capability:
        if _local_snapshot() is None:
            return Capability(False, _MISSING)
        for module in ("torch", "transformers"):
            if importlib.util.find_spec(module) is None:
                return Capability(False, f"Prompt Guard 2 dependency not installed: {module}")
        return Capability(True)

    def inspect(self, src: Path, ctx: RunContext) -> list[Finding]:
        snapshot = _local_snapshot()
        if snapshot is None:
            raise AdapterError(_MISSING)
        if not ctx.channels:
            return []
        runner = Path(__file__).with_name("_pg_runner.py")
        payload = {"snapshot": str(snapshot), "channels": [c.text for c in ctx.channels]}
        proc = run_tool([sys.executable, str(runner)], ctx, stdin=json.dumps(payload), ok_codes=(0,))
        try:
            rows = json.loads(proc.stdout)
            if not isinstance(rows, list):
                raise TypeError("expected a list")
            best: dict[int, dict] = {}
            for row in rows:
                index, score = row["index"], float(row["score"])
                if (not isinstance(index, int) or not 0 <= index < len(ctx.channels)
                        or not math.isfinite(score) or not 0 <= score <= 1):
                    raise ValueError("invalid index or score")
                if index not in best or score > float(best[index]["score"]):
                    best[index] = row
            if set(best) != set(range(len(ctx.channels))):
                raise ValueError("missing channel scores")
            out = []
            for index in sorted(best):
                row, channel = best[index], ctx.channels[index]
                score = float(row["score"])
                if score < 0.5:
                    continue
                cls = {"filename": "filename_injection", "metadata": "metadata_injection",
                       "alt": "link_or_alt_injection", "hyperlink": "link_or_alt_injection"}.get(
                           channel.channel, "prompt_injection")
                out.append(Finding(
                    adapter=self.id, adapter_code="PROMPT_GUARD_2", cls=cls,
                    severity="critical" if score >= 0.9 else "high", confidence="model",
                    location=Location(file=src.name, channel=channel.channel, page=channel.page,
                                      pointer=channel.pointer), excerpt=channel.text,
                    evidence={"score": score, "decision": row["decision"],
                              "hidden": channel.hidden}))
            return out
        except (ValueError, TypeError, KeyError) as exc:
            raise AdapterError(f"invalid Prompt Guard output: {exc}") from exc

    def clean(self, src: Path, dest: Path, ctx: RunContext):
        raise AdapterError("Prompt Guard is detect-only")
