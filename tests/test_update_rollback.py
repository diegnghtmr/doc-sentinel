"""S7: updates exercise real, disposable local Git remotes, never the project's pins."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import zlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from doc_sentinel import cli
from doc_sentinel.adapters.base import RunContext, run_tool

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def git(workdir: Path, *args: str) -> str:
    proc = run_tool(["git", "-c", "protocol.file.allow=always", *args],
                    RunContext(workdir=workdir, timeout_s=30), cwd=workdir, ok_codes=[0])
    return proc.stdout.strip()


def _object(git_dir: Path, kind: str, data: bytes) -> str:
    """Create disposable fixture objects without staging or running git commit."""
    raw = f"{kind} {len(data)}\0".encode() + data
    sha = hashlib.sha1(raw).hexdigest()
    target = git_dir / "objects" / sha[:2] / sha[2:]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(zlib.compress(raw))
    return sha


def fixture_commit(git_dir: Path, files: dict[str, str], subject: str,
                   parent: str | None = None, links: dict[str, str] | None = None) -> str:
    tree: dict = {}
    for name, content in files.items():
        parts = name.split("/")
        current = tree
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        current[parts[-1]] = ("100644", _object(git_dir, "blob", content.encode()))
    for name, sha in (links or {}).items():
        parts = name.split("/")
        current = tree
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        current[parts[-1]] = ("160000", sha)

    def write_tree(entries: dict) -> str:
        output = b""
        for name, entry in sorted(entries.items()):
            mode, sha = ("40000", write_tree(entry)) if isinstance(entry, dict) else entry
            output += f"{mode} {name}\0".encode() + bytes.fromhex(sha)
        return _object(git_dir, "tree", output)

    parent_line = f"parent {parent}\n" if parent else ""
    commit = (f"tree {write_tree(tree)}\n{parent_line}"
              "author Fixture <fixture@example.invalid> 1700000000 +0000\n"
              "committer Fixture <fixture@example.invalid> 1700000000 +0000\n\n"
              f"{subject}\n")
    sha = _object(git_dir, "commit", commit.encode())
    ref = git_dir / "refs" / "heads" / "main"
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text(sha + "\n", encoding="ascii")
    return sha


class Repos:
    def __init__(self, base: Path, ids: tuple[str, ...] = ("A", "B"), shallow=()):
        self.root = base / "parent"
        self.remotes = {}
        self.old = {}
        self.new = {}
        pins = []
        modules = []
        for pin_id in ids:
            remote = base / f"upstream-{pin_id}.git"
            git(base, "init", "-q", "--bare", "-b", "main", str(remote))
            old = fixture_commit(remote, {"marker.txt": "old\n",
                                          "LlamaFirewall/keep.txt": "old\n",
                                          "outside/omit.txt": "outside\n"},
                                 f"initial {pin_id}")
            path = f"third_party/{pin_id}"
            url = remote.as_uri()
            modules.append(f'[submodule "{path}"]\n\tpath = {path}\n\turl = {url}\n'
                           "\tbranch = main\n")
            pins.append({"id": pin_id, "url": url, "branch": "main", "sha": old,
                         "path": path, "inspected_at": "2020-01-01T00:00:00+00:00"})
            self.remotes[pin_id], self.old[pin_id] = remote, old
        modules_text = "".join(modules)
        parent_remote = base / "upstream-parent.git"
        git(base, "init", "-q", "--bare", "-b", "main", str(parent_remote))
        fixture_commit(parent_remote, {".gitmodules": modules_text}, "parent fixture",
                       links={f"third_party/{pin_id}": self.old[pin_id] for pin_id in ids})
        # Cloning the parent creates a clean index with real gitlinks, without staging changes.
        git(base, "clone", "-q", parent_remote.as_uri(), str(self.root))
        for pin in pins:
            pin_id = pin["id"]
            dest = self.root / pin["path"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            git_dir = self.root / ".git" / "modules" / "third_party" / pin_id
            git_dir.parent.mkdir(parents=True, exist_ok=True)
            args = ["clone", "-q", "--separate-git-dir", str(git_dir)]
            if pin_id in shallow:
                args.extend(["--depth", "1"])
            git(base, *args, pin["url"], str(dest))
            git(self.root, "config", f"submodule.{pin['path']}.url", pin["url"])
        (self.root / "pins.lock.json").write_text(
            json.dumps({"schema_version": 1, "branch_policy": "main-or-default", "pins": pins},
                       indent=2) + "\n", encoding="utf-8")
        (self.root / "UPDATES.md").write_text("# Updates\n\nExisting entry.\n", encoding="utf-8")

    def advance(self, pin_id: str, subject: str | None = None, *, broken=False) -> str:
        parent = self.new.get(pin_id, self.old[pin_id])
        sha = fixture_commit(self.remotes[pin_id],
                             {"marker.txt": "BROKEN\n" if broken else "new\n",
                              "LlamaFirewall/keep.txt": "new\n",
                              "outside/omit.txt": "outside\n"},
                             subject or f"advance {pin_id}", parent)
        self.new[pin_id] = sha
        return sha

    def head(self, pin_id: str) -> str:
        return git(self.root, "-C", str(self.root / "third_party" / pin_id), "rev-parse", "HEAD")

    def lock(self) -> dict:
        return json.loads((self.root / "pins.lock.json").read_text(encoding="utf-8"))


@pytest.fixture
def repos(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    with tempfile.TemporaryDirectory(prefix="update-fixture-", dir=tmp_path) as directory:
        yield Repos(Path(directory))


def update(*args, **kwargs):
    from doc_sentinel.update import run_update

    return run_update(*args, **kwargs)


def corpus_command() -> list[str]:
    script = (
        "import os, pathlib, sys; "
        "p = pathlib.Path('third_party/B/marker.txt'); "
        "assert os.environ['DOC_SENTINEL_HOME'] == str(pathlib.Path.cwd()); "
        "broken = p.exists() and p.read_text() == 'BROKEN\\n'; "
        "print('corpus B broken' if broken else 'corpus passed'); "
        "sys.exit(1 if broken else 0)"
    )
    return [sys.executable, "-c", script]


def test_good_pin_stays_updated_when_later_pin_rolls_back(repos):
    for name in ("A", "B"):
        assert git(repos.root, "ls-files", "-s", f"third_party/{name}") == (
            f"160000 {repos.old[name]} 0\tthird_party/{name}")
    repos.advance("A", "A good change")
    repos.advance("B", "B bad change", broken=True)
    builds = []
    rebuild = {pin_id: (lambda path, name=pin_id: builds.append((name, repos.head(name))))
               for pin_id in ("A", "B")}
    result, code = update(repos.root, test_cmd=corpus_command(), rebuild=rebuild, now=NOW)
    assert code == 1
    assert result["mode"] == "update" and result["started_at"] == NOW.isoformat()
    assert result["dry_run"] is False
    a, b = result["pins"]
    assert (a["id"], a["status"], a["old"], a["new"], a["tests"], a["reason"]) == (
        "A", "updated", repos.old["A"], repos.new["A"], "passed", None)
    assert (b["id"], b["status"], b["old"], b["new"], b["tests"]) == (
        "B", "rolled_back", repos.old["B"], repos.new["B"], "failed")
    assert "corpus B broken" in b["reason"]
    assert len(a["changelog"]) == len(b["changelog"]) == 1
    assert a["changelog"][0].endswith(" A good change")
    assert b["changelog"][0].endswith(" B bad change")
    assert repos.head("A") == repos.new["A"] and repos.head("B") == repos.old["B"]
    a_lock, b_lock = repos.lock()["pins"]
    assert a_lock["sha"] == repos.new["A"] and a_lock["inspected_at"] == NOW.isoformat()
    assert b_lock["sha"] == repos.old["B"] and b_lock["inspected_at"] == "2020-01-01T00:00:00+00:00"
    assert builds == [("A", repos.new["A"]), ("B", repos.new["B"]), ("B", repos.old["B"])]
    log = (repos.root / "UPDATES.md").read_text(encoding="utf-8")
    for expected in ("Existing entry.", "2026-01-02", "A good change", "B bad change",
                     "updated", "rolled_back", "corpus B broken", repos.old["A"][:7],
                     repos.new["A"][:7], repos.old["B"][:7], repos.new["B"][:7]):
        assert expected in log


def test_unchanged_pins_skip_corpus_and_leave_lock_unchanged(repos):
    before = (repos.root / "pins.lock.json").read_bytes()
    result, code = update(repos.root, test_cmd=["command-that-does-not-exist"], now=NOW)
    assert code == 0
    assert [(p["id"], p["status"], p["tests"], p["reason"]) for p in result["pins"]] == [
        ("A", "unchanged", "skipped", None), ("B", "unchanged", "skipped", None)]
    assert (repos.root / "pins.lock.json").read_bytes() == before
    assert [repos.head(name) for name in ("A", "B")] == [repos.old["A"], repos.old["B"]]
    log = (repos.root / "UPDATES.md").read_text(encoding="utf-8")
    assert "2026-01-02" in log and "unchanged" in log


def test_only_selects_one_pin_without_fetching_or_changing_other(repos):
    repos.advance("A")
    repos.advance("B")
    result, code = update(repos.root, only="A", test_cmd=corpus_command(), now=NOW)
    assert code == 0 and [(p["id"], p["status"]) for p in result["pins"]] == [("A", "updated")]
    assert repos.head("A") == repos.new["A"] and repos.head("B") == repos.old["B"]
    assert [p["sha"] for p in repos.lock()["pins"]] == [repos.new["A"], repos.old["B"]]
    remote_b = git(repos.root, "-C", str(repos.root / "third_party/B"),
                   "rev-parse", "refs/remotes/origin/main")
    assert remote_b == repos.old["B"]


def test_dry_run_fetches_and_reports_without_checkout_rebuild_or_file_writes(repos):
    repos.advance("A", "A dry change")
    before_lock = (repos.root / "pins.lock.json").read_bytes()
    before_log = (repos.root / "UPDATES.md").read_bytes()
    def forbidden(_path):
        pytest.fail("dry run rebuilt an artifact")
    result, code = update(repos.root, dry_run=True, test_cmd=["missing-test-command"],
                          rebuild={"A": forbidden}, now=NOW)
    assert code == 0 and result["dry_run"] is True
    a, b = result["pins"]
    assert (a["status"], a["tests"], a["old"], a["new"], a["reason"]) == (
        "would_update", "skipped", repos.old["A"], repos.new["A"], None)
    assert a["changelog"][0].endswith(" A dry change")
    assert b["status"] == "unchanged" and b["tests"] == "skipped"
    assert [repos.head(name) for name in ("A", "B")] == [repos.old["A"], repos.old["B"]]
    assert (repos.root / "pins.lock.json").read_bytes() == before_lock
    assert (repos.root / "UPDATES.md").read_bytes() == before_log


def test_unknown_only_returns_cli_error_without_any_changes(repos, monkeypatch, capsys):
    monkeypatch.setattr(cli, "repo_root", lambda: repos.root)
    before = [(repos.root / name).read_bytes() for name in ("pins.lock.json", "UPDATES.md")]
    code = cli.main(["update", "--only", "missing", "--json"])
    out = capsys.readouterr()
    assert code == 2 and "missing" in out.out + out.err
    assert "unknown" in (out.out + out.err).lower()
    assert [(repos.root / name).read_bytes() for name in ("pins.lock.json", "UPDATES.md")] == before
    assert [repos.head(name) for name in ("A", "B")] == [repos.old["A"], repos.old["B"]]


def test_fetch_error_returns_2_and_preserves_pin(repos):
    missing = (repos.root.parent / "missing.git").as_uri()
    git(repos.root, "-C", str(repos.root / "third_party/A"), "remote", "set-url", "origin", missing)
    lock = repos.lock()
    lock["pins"][0]["url"] = missing
    (repos.root / "pins.lock.json").write_text(json.dumps(lock), encoding="utf-8")
    before = (repos.root / "pins.lock.json").read_bytes()
    result, code = update(repos.root, only="A", now=NOW)
    assert code == 2
    a = result["pins"][0]
    assert (a["id"], a["status"], a["old"], a["new"], a["tests"]) == (
        "A", "fetch_error", repos.old["A"], None, "skipped")
    assert a["reason"] and repos.head("A") == repos.old["A"]
    assert (repos.root / "pins.lock.json").read_bytes() == before
    assert "fetch_error" in (repos.root / "UPDATES.md").read_text(encoding="utf-8")


def test_remote_must_match_pinned_url_before_fetch(repos):
    # The other fixture remote exists: fetching it instead of the lock's URL would violate policy.
    wrong = repos.remotes["B"].as_uri()
    git(repos.root, "-C", str(repos.root / "third_party/A"), "remote", "set-url", "origin", wrong)
    before = (repos.root / "pins.lock.json").read_bytes()
    result, code = update(repos.root, only="A", dry_run=True, now=NOW)
    assert code == 2 and result["pins"][0]["status"] == "fetch_error"
    assert "remote" in result["pins"][0]["reason"].lower()
    assert repos.head("A") == repos.old["A"]
    assert (repos.root / "pins.lock.json").read_bytes() == before


def test_sparse_shallow_pin_has_complete_capped_changelog_and_preserves_sparse_checkout(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    with tempfile.TemporaryDirectory(prefix="update-shallow-", dir=tmp_path) as directory:
        repos = Repos(Path(directory), ids=("PurpleLlama",), shallow=("PurpleLlama",))
        sub = repos.root / "third_party/PurpleLlama"
        git(repos.root, "-C", str(sub), "sparse-checkout", "init", "--cone")
        git(repos.root, "-C", str(sub), "sparse-checkout", "set", "LlamaFirewall")
        for i in range(33):
            repos.advance("PurpleLlama", f"change {i:02d}")
        result, code = update(repos.root, test_cmd=corpus_command(),
                              rebuild={"PurpleLlama": lambda path: None}, now=NOW)
        assert code == 0 and result["pins"][0]["status"] == "updated"
        changes = result["pins"][0]["changelog"]
        assert len(changes) == 30
        assert changes[0].endswith(" change 32") and changes[-1].endswith(" change 03")
        assert repos.head("PurpleLlama") == repos.new["PurpleLlama"]
        assert (sub / "LlamaFirewall/keep.txt").read_text() == "new\n"
        assert not (sub / "outside/omit.txt").exists()
        assert repos.lock()["pins"][0]["sha"] == repos.new["PurpleLlama"]


def test_rebuild_failure_rolls_back_and_is_operational_error(repos):
    repos.advance("A")
    seen = []
    def rebuild(path):
        seen.append(repos.head("A"))
        if repos.head("A") == repos.new["A"]:
            raise RuntimeError("build failed")
    before = (repos.root / "pins.lock.json").read_bytes()
    result, code = update(repos.root, only="A", rebuild={"A": rebuild}, now=NOW)
    assert code == 2
    assert result["pins"][0]["status"] == "rolled_back"
    assert "build failed" in result["pins"][0]["reason"]
    assert result["pins"][0]["tests"] == "skipped"
    assert repos.head("A") == repos.old["A"] and seen == [repos.new["A"], repos.old["A"]]
    assert (repos.root / "pins.lock.json").read_bytes() == before


def test_cli_dry_run_only_json_and_human_output(repos, monkeypatch, capsys):
    repos.advance("A")
    monkeypatch.setattr(cli, "repo_root", lambda: repos.root)
    code = cli.main(["update", "--dry-run", "--only", "A", "--json"])
    out = capsys.readouterr()
    report = json.loads(out.out)
    assert code == 0 and out.err == ""
    assert [(p["id"], p["status"]) for p in report["pins"]] == [("A", "would_update")]
    code = cli.main(["update", "--dry-run", "--only", "B"])
    out = capsys.readouterr()
    assert code == 0 and out.out == "B: unchanged\n" and out.err == ""


def test_default_artifact_rebuild_commands_are_offline(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    from doc_sentinel import update as update_module

    ids = ("chk_defaced-rs", "subtext-scan", "perlecta-mcp", "unicode-smuggling-guard",
           "PurpleLlama", "watermarks-remover", "hidden-text-detector")
    with tempfile.TemporaryDirectory(prefix="update-build-", dir=tmp_path) as directory:
        repos = Repos(Path(directory), ids=ids)
        for name in ids:
            repos.advance(name)
        calls = []
        real_run = update_module.run_tool
        def fake_run(cmd, ctx, **kwargs):
            if cmd[0] in {"cargo", "npm", "/fixture/uv"}:
                calls.append((list(cmd), ctx.allow_network, kwargs))
                if cmd[0] == "cargo":
                    target = Path(kwargs["extra_env"]["CARGO_TARGET_DIR"]) / "release"
                    target.mkdir(parents=True, exist_ok=True)
                    (target / "chk_defaced").write_bytes(b"fixture binary")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return real_run(cmd, ctx, **kwargs)
        monkeypatch.setattr(update_module, "run_tool", fake_run)
        monkeypatch.setattr(update_module, "venv_bin", lambda name: "/fixture/uv")
        report, code = update_module.run_update(repos.root, test_cmd=corpus_command(), now=NOW)
        assert code == 0 and [p["status"] for p in report["pins"]] == ["updated"] * len(ids)
        assert (repos.root / "bin/chk_defaced").read_bytes() == b"fixture binary"
        assert len(calls) == 5 and all(not network for _, network, _ in calls)
        cargo, npm, *uv_calls = calls
        assert cargo[0] == ["cargo", "build", "--release"]
        assert cargo[2]["cwd"] == repos.root / "third_party/chk_defaced-rs"
        assert cargo[2]["extra_env"]["CARGO_NET_OFFLINE"] == "true"
        assert cargo[2]["extra_env"]["CARGO_TARGET_DIR"] == str(repos.root / ".cache/cargo")
        assert npm[0] == ["npm", "ci", "--ignore-scripts"]
        assert npm[2]["extra_env"]["npm_config_offline"] == "true"
        assert [call[0][-1] for call in uv_calls] == [
            str(repos.root / "third_party/perlecta-mcp"),
            str(repos.root / "third_party/unicode-smuggling-guard"),
            str(repos.root / "third_party/PurpleLlama/LlamaFirewall"),
        ]
        assert all(call[0][1:-1] == ["pip", "install", "-p", sys.executable, "-e"]
                   and call[2]["extra_env"]["UV_OFFLINE"] == "1" for call in uv_calls)


def test_missing_test_command_rolls_back_and_is_operational_error(repos):
    repos.advance("A")
    before = (repos.root / "pins.lock.json").read_bytes()
    result, code = update(repos.root, only="A", test_cmd=["no-such-corpus-command"], now=NOW)
    assert code == 2 and result["pins"][0]["status"] == "rolled_back"
    assert "cannot execute" in result["pins"][0]["reason"]
    assert result["pins"][0]["tests"] == "skipped"
    assert repos.head("A") == repos.old["A"]
    assert (repos.root / "pins.lock.json").read_bytes() == before


def test_dirty_checkout_is_not_overwritten(repos):
    repos.advance("A")
    marker = repos.root / "third_party/A/marker.txt"
    marker.write_text("local edit\n", encoding="utf-8")
    before = (repos.root / "pins.lock.json").read_bytes()
    result, code = update(repos.root, only="A", test_cmd=corpus_command(), now=NOW)
    assert code == 2 and result["pins"][0]["status"] == "error"
    assert "local changes" in result["pins"][0]["reason"]
    assert marker.read_text(encoding="utf-8") == "local edit\n"
    assert repos.head("A") == repos.old["A"]
    assert (repos.root / "pins.lock.json").read_bytes() == before
