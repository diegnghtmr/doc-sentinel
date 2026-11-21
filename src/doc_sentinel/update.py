"""Update pinned submodules one at a time, retaining only corpus-verified changes.

Fetch is the only network-enabled subprocess. Rebuilds and corpus tests use the adapter
runner's offline environment and, where available, its network namespace sandbox.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from doc_sentinel.adapters.base import AdapterError, RunContext, run_tool, venv_bin
from doc_sentinel.pins import load_lock, save_lock

_TEST_LOG_TAIL = 4000


def _git(path: Path, ctx: RunContext, *args: str, network: bool = False) -> str:
    git_ctx = RunContext(workdir=ctx.workdir, timeout_s=ctx.timeout_s, allow_network=network)
    proc = run_tool(["git", "-c", "http.followRedirects=false", "-C", str(path), *args],
                    git_ctx, ok_codes=[0], extra_env={
        "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_LFS_SKIP_SMUDGE": "1",
    })
    return proc.stdout.strip()


def _default_rebuild(pin_id: str, path: Path, root: Path, ctx: RunContext) -> None:
    if pin_id == "chk_defaced-rs":
        target = root / ".cache" / "cargo"
        run_tool(["cargo", "build", "--release"], ctx, cwd=path, ok_codes=[0], extra_env={
            "CARGO_TARGET_DIR": str(target), "CARGO_NET_OFFLINE": "true",
        })
        binary = root / "bin" / "chk_defaced"
        binary.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target / "release" / "chk_defaced", binary)
    elif pin_id == "subtext-scan":
        run_tool(["npm", "ci", "--ignore-scripts"], ctx, cwd=path, ok_codes=[0], extra_env={
            "npm_config_offline": "true", "npm_config_audit": "false",
            "npm_config_fund": "false", "npm_config_update_notifier": "false",
        })
    elif pin_id in {"perlecta-mcp", "unicode-smuggling-guard", "PurpleLlama"}:
        uv = venv_bin("uv")
        if uv:
            package = path / "LlamaFirewall" if pin_id == "PurpleLlama" else path
            run_tool([uv, "pip", "install", "-p", sys.executable, "-e", str(package)],
                     ctx, cwd=root, ok_codes=[0], extra_env={"UV_OFFLINE": "1"})


def _rebuild(pin_id: str, path: Path, root: Path, ctx: RunContext,
             overrides: Mapping[str, Callable[[Path], None]]) -> None:
    if pin_id in overrides:
        overrides[pin_id](path)
    else:
        _default_rebuild(pin_id, path, root, ctx)


def _append_update(root: Path, timestamp: str, item: dict[str, Any]) -> None:
    old = (item["old"] or "unknown")[:12]
    new = (item["new"] or "unknown")[:12]
    lines = ["", f"## {timestamp}", "", f"### {item['id']}: {old} -> {new}", "",
             f"- Status: {item['status']}", f"- Tests: {item['tests']}"]
    if item["changelog"]:
        lines.extend(["- Changelog:", *(f"  - {line}" for line in item["changelog"])])
    else:
        lines.append("- Changelog: none")
    if item["reason"]:
        # Indent each line as data, including multi-line test output.
        lines.append("- Reason:")
        lines.extend(f"    {line}" for line in item["reason"].splitlines())
    with (root / "UPDATES.md").open("a", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")


def _error(report: dict[str, Any], message: str) -> tuple[dict[str, Any], int]:
    report["error"] = message
    print(f"doc-sentinel: {message}", file=sys.stderr)
    return report, 2


def run_update(root: Path, *, dry_run: bool = False, only: str | None = None,
               test_cmd: Sequence[str] | None = None,
               rebuild: Mapping[str, Callable[[Path], None]] | None = None,
               now: datetime | None = None) -> tuple[dict[str, Any], int]:
    """Fetch and inspect each pin, rolling back only the pin that fails verification.

    ``rebuild`` overrides selected artifact rebuilds with callables receiving the
    submodule path. ``test_cmd`` is run in the parent root after each checkout.
    """
    root = Path(root).resolve()
    instant = now or datetime.now().astimezone()
    if instant.utcoffset() is None:
        instant = instant.astimezone()
    timestamp = instant.isoformat(timespec="seconds")
    report: dict[str, Any] = {"mode": "update", "started_at": timestamp,
                              "dry_run": dry_run, "pins": []}
    try:
        lock = load_lock(root)
        pins = lock["pins"]
        if not isinstance(pins, list):
            raise TypeError("pins must be a list")
        for pin in pins:
            if not all(isinstance(pin.get(k), str) and pin[k]
                       for k in ("id", "url", "branch", "sha", "path")):
                raise ValueError("every pin requires id, url, branch, sha and path")
        if len({p["id"] for p in pins}) != len(pins):
            raise ValueError("duplicate pin id")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return _error(report, f"cannot read pins.lock.json: {exc}")
    if only is not None and not any(pin["id"] == only for pin in pins):
        return _error(report, f"unknown pin id: {only}")

    command = list(test_cmd) if test_cmd is not None else [sys.executable, "-m", "pytest", "-q", "tests"]
    overrides = rebuild or {}
    exit_code = 0
    with tempfile.TemporaryDirectory(prefix="doc-sentinel-update-") as scratch:
        ctx = RunContext(workdir=Path(scratch), timeout_s=1800)
        for pin in pins:
            pin_id = pin["id"]
            if only is not None and pin_id != only:
                continue
            old = pin["sha"]
            item: dict[str, Any] = {"id": pin_id, "status": "fetch_error", "old": old,
                                    "new": None, "changelog": [], "tests": "skipped",
                                    "reason": None}
            report["pins"].append(item)
            path = (root / pin["path"]).resolve()
            try:
                if not path.is_relative_to(root) or path == root or not path.is_dir():
                    raise ValueError("pin path is not a subdirectory of the repository")
                branch = pin["branch"]
                if branch.startswith("-"):
                    raise ValueError("invalid branch name")
                _git(path, ctx, "check-ref-format", f"refs/heads/{branch}")
                urls = _git(path, ctx, "remote", "get-url", "--all", "origin").splitlines()
                if urls != [pin["url"]]:
                    raise ValueError("origin remote does not match the pinned URL")
                shallow = _git(path, ctx, "rev-parse", "--is-shallow-repository") == "true"
                _git(path, ctx, "fetch", "--no-recurse-submodules", "--no-tags", "origin", branch,
                     network=True)
                new = _git(path, ctx, "rev-parse", "--verify", f"refs/remotes/origin/{branch}^{{commit}}")
                fetched = _git(path, ctx, "rev-parse", "--verify", "FETCH_HEAD^{commit}")
                if new != fetched:
                    raise ValueError("origin tracking ref does not match the fetched branch")
                item["new"] = new
                if new == old:
                    item["status"] = "unchanged"
                else:
                    if shallow:
                        _git(path, ctx, "fetch", "--no-recurse-submodules", "--no-tags",
                             "--unshallow", "origin", branch, network=True)
                        # A branch can advance while fetching history: never test a stale tip.
                        latest = _git(path, ctx, "rev-parse", "--verify",
                                      f"refs/remotes/origin/{branch}^{{commit}}")
                        if latest != new:
                            raise ValueError("branch advanced while fetching history; retry update")
                    item["status"] = "error"
                    item["changelog"] = _git(path, ctx, "log", "--max-count=30", "--format=%h %s",
                                             f"{old}..{new}").splitlines()
                    if dry_run:
                        item["status"] = "would_update"
                    else:
                        if _git(path, ctx, "rev-parse", "HEAD") != old:
                            raise ValueError("checkout does not match the locked SHA")
                        if _git(path, ctx, "status", "--porcelain", "--untracked-files=no"):
                            raise ValueError("submodule has tracked local changes")
                        _git(path, ctx, "checkout", "--detach", new)
                        try:
                            _rebuild(pin_id, path, root, ctx, overrides)
                            if not command or shutil.which(command[0]) is None:
                                raise OSError(f"cannot execute test command: {command[0] if command else ''}")
                            proc = run_tool(command, ctx, cwd=root,
                                            extra_env={"DOC_SENTINEL_HOME": str(root)})
                            if proc.returncode:
                                item["tests"] = "failed"
                                output = "\n".join(s.strip() for s in (proc.stdout, proc.stderr) if s.strip())
                                item["reason"] = (f"tests failed (exit {proc.returncode}):\n"
                                                  f"{output[-_TEST_LOG_TAIL:]}")
                                failure_code = 1
                            else:
                                item["tests"] = "passed"
                                previous_inspected = pin.get("inspected_at")
                                pin["sha"], pin["inspected_at"] = new, timestamp
                                try:
                                    save_lock(root, lock)
                                except Exception:
                                    pin["sha"] = old
                                    if previous_inspected is None:
                                        pin.pop("inspected_at", None)
                                    else:
                                        pin["inspected_at"] = previous_inspected
                                    raise
                                item["status"] = "updated"
                                failure_code = 0
                        except Exception as exc:  # noqa: BLE001 - roll back operational failures too
                            item["reason"] = f"operational failure: {type(exc).__name__}: {exc}"
                            failure_code = 2
                        if failure_code:
                            try:
                                _git(path, ctx, "checkout", "--detach", old)
                                item["status"] = "rolled_back"
                                _rebuild(pin_id, path, root, ctx, overrides)
                            except Exception as exc:  # noqa: BLE001 - report incomplete restoration
                                if item["status"] != "rolled_back":
                                    item["status"] = "error"
                                item["reason"] += f"\nrollback failed: {type(exc).__name__}: {exc}"
                                failure_code = 2
                            exit_code = max(exit_code, failure_code)
            except (AdapterError, OSError, ValueError) as exc:
                item["reason"] = f"{type(exc).__name__}: {exc}"
                exit_code = 2
            if not dry_run:
                try:
                    _append_update(root, timestamp, item)
                except OSError as exc:
                    message = f"cannot append UPDATES.md: {exc}"
                    item["reason"] = f"{item['reason']}\n{message}" if item["reason"] else message
                    exit_code = 2
                    break
    return report, exit_code
