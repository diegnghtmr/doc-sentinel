"""S1: pins.lock.json and the git submodule gitlinks agree (PLAN Fase 0, CI check)."""

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLAN_SHAS = {
    "watermarks-remover": "5a62be6338d2812372b1da440e55c82922f97d53",
    "perlecta-mcp": "478d8061304c67798a3f9fee4b59f08594c1e003",
    "hidden-text-detector": "ce9e505dc1c128701e921e8d7e1a133c7ff28db8",
    "subtext-scan": "c237a68b4498dfc29cec0a5b1370f4b7b2743ba7",
    "chk_defaced-rs": "d7a4b0998b564dc4a34c84bc88622aaaa105251f",
    "unicode-smuggling-guard": "0cedc4743f77a9d1f7fe94fa99571efb968ba401",
    "PurpleLlama": "0654501399e7cbd560db9501965dc09c567a5563",
}


def lock():
    return json.loads((ROOT / "pins.lock.json").read_text())


def test_lockfile_shape():
    data = lock()
    assert data["schema_version"] == 1 and data["branch_policy"] == "main-or-default"
    for pin in data["pins"]:
        assert set(pin) >= {"id", "url", "branch", "sha", "path", "inspected_at"}
        assert len(pin["sha"]) == 40 and pin["branch"] == "main"
        assert pin["path"] == f"third_party/{pin['id']}"


def _checkout_sha(path: Path) -> str | None:
    out = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=False)
    return out.stdout.strip() if out.returncode == 0 else None


def test_every_pin_is_a_submodule_gitlink_at_the_locked_sha():
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    for pin in lock()["pins"]:
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-s", pin["path"]],
                             capture_output=True, text=True, check=True).stdout.split()
        assert out[0] == "160000", f"{pin['id']} is not a submodule gitlink"
        # `doc-sentinel update` moves the lock and the checkout but never stages the gitlink
        # (the human commits it), so either the indexed gitlink or the checkout must match.
        actual = {out[1], _checkout_sha(ROOT / pin["path"])}
        assert pin["sha"] in actual, f"{pin['id']}: gitlink/checkout {actual} != lock {pin['sha']}"


def test_initial_pins_match_plan_until_first_update():
    # Guards accidental edits; `doc-sentinel update` legitimately moves pins and updates this
    # expectation together with UPDATES.md.
    current = {p["id"]: p["sha"] for p in lock()["pins"]}
    if "## 20" in (ROOT / "UPDATES.md").read_text().split("initial pins", 1)[-1]:
        pytest.skip("pins already moved by doc-sentinel update")
    assert current == PLAN_SHAS
