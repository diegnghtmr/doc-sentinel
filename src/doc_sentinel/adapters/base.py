"""Adapter contract (PLAN §7) and the only sanctioned way to launch a sub-harness process.

Adapters never call ``subprocess`` directly: :func:`run_tool` scrubs the environment
(no Layer B variables, no credentials), forces Hugging Face offline mode unless network is
allowed, applies a timeout, never uses a shell, and — when the kernel allows unprivileged
user namespaces — runs the child in a fresh network namespace with no interfaces.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from doc_sentinel.schema import Channel, Finding

# Environment variables a child may inherit. Everything else (tokens, API keys, Layer B
# configuration such as WATERMARKS_REWRITE_* / WATERMARKS_CLEAN_STRATEGY_FILE) is dropped.
_ENV_ALLOW = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "HF_HOME",
              "HF_HUB_CACHE", "TRANSFORMERS_CACHE", "TESSDATA_PREFIX", "DOC_SENTINEL_HOME")


class AdapterError(RuntimeError):
    """The sub-harness crashed, timed out or produced unparseable output."""


@dataclass
class Capability:
    available: bool
    reason: str | None = None
    features: dict[str, bool] = field(default_factory=dict)


@dataclass
class RunContext:
    workdir: Path
    timeout_s: float = 180.0
    allow_network: bool = False
    ocr_lang: str = "spa+eng"
    channels: list[Channel] = field(default_factory=list)
    log: Callable[[str], None] = lambda message: None


@dataclass
class CleanResult:
    changed: bool
    actions: list[dict[str, Any]] = field(default_factory=list)
    c2pa_before: bool | None = None
    c2pa_after: bool | None = None
    residual: bool = False
    warnings: list[str] = field(default_factory=list)


class Adapter(Protocol):
    id: str
    mode: frozenset[str]

    def available(self) -> Capability: ...

    def inspect(self, src: Path, ctx: RunContext) -> list[Finding]: ...

    def clean(self, src: Path, dest: Path, ctx: RunContext) -> CleanResult: ...


def repo_root() -> Path:
    """Root of the doc-sentinel checkout (holds ``pins.lock.json`` and ``third_party/``)."""
    env = os.environ.get("DOC_SENTINEL_HOME")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pins.lock.json").is_file():
            return parent
    return Path.cwd().resolve()


def third_party(name: str) -> Path:
    return repo_root() / "third_party" / name


def venv_bin(name: str) -> str | None:
    """Executable installed next to the running interpreter, else on PATH."""
    candidate = Path(sys.executable).parent / name
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return shutil.which(name)


@functools.lru_cache(maxsize=1)
def network_sandbox_available() -> bool:
    if shutil.which("unshare") is None:
        return False
    try:
        probe = subprocess.run(["unshare", "-rn", "true"], capture_output=True, timeout=10,
                               check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def child_env(ctx: RunContext, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: os.environ[k] for k in _ENV_ALLOW if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.setdefault("LANG", "C.UTF-8")
    env["HOME"] = str(ctx.workdir)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if not ctx.allow_network:
        env["HF_HUB_OFFLINE"] = "1"
        env["TRANSFORMERS_OFFLINE"] = "1"
        env["HF_HUB_DISABLE_TELEMETRY"] = "1"
    if extra:
        env.update(extra)
    return env


def run_tool(cmd: Sequence[str], ctx: RunContext, *, cwd: Path | None = None,
             extra_env: dict[str, str] | None = None, stdin: str | None = None,
             ok_codes: Sequence[int] | None = None) -> subprocess.CompletedProcess[str]:
    """Run one sub-harness command. Raises :class:`AdapterError` on timeout or bad exit code.

    ``ok_codes`` lists acceptable exit codes (default: any — many scanners exit 1 when they
    find something, so adapters decide).
    """
    argv = [str(c) for c in cmd]
    if not ctx.allow_network:
        if os.environ.get("DOC_SENTINEL_NO_NETNS") == "1":
            pass  # isolation is declared external (e.g. container with --network=none)
        elif network_sandbox_available():
            argv = ["unshare", "-rn", "--", *argv]
        else:
            # Fail closed: never run an offline step with network access.
            raise AdapterError(
                "network isolation unavailable (unshare -rn failed); run inside a container "
                "with --network=none and DOC_SENTINEL_NO_NETNS=1, or pass --allow-network")
    ctx.log("run: " + " ".join(argv))
    try:
        proc = subprocess.run(argv, cwd=str(cwd or ctx.workdir), env=child_env(ctx, extra_env),
                              input=stdin, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=ctx.timeout_s, check=False)
    except subprocess.TimeoutExpired as exc:
        raise AdapterError(f"timeout after {ctx.timeout_s:.0f}s: {argv[-1]}") from exc
    except OSError as exc:
        raise AdapterError(f"cannot execute {argv[0]}: {exc}") from exc
    if ok_codes is not None and proc.returncode not in ok_codes:
        tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        raise AdapterError(f"exit {proc.returncode}: {tail}")
    return proc
