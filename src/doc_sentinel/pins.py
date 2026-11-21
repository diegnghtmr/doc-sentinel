"""pins.lock.json access (PLAN §5.1)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

LOCK_NAME = "pins.lock.json"


def load_lock(root: Path) -> dict[str, Any]:
    return json.loads((root / LOCK_NAME).read_text(encoding="utf-8"))


def save_lock(root: Path, lock: dict[str, Any]) -> None:
    path = root / LOCK_NAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def checkout_sha(root: Path, rel_path: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(root / rel_path), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def pin_summary(root: Path) -> list[dict[str, Any]]:
    try:
        lock = load_lock(root)
    except (OSError, ValueError):
        return []
    return [{"id": p["id"], "sha": p["sha"]} for p in lock.get("pins", [])]


def tool_version(root: Path, fallback: str) -> str:
    try:
        out = subprocess.run(["git", "-C", str(root), "describe", "--always", "--dirty",
                              "--tags"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return fallback
    described = out.stdout.strip()
    return f"{fallback}+{described}" if out.returncode == 0 and described else fallback
