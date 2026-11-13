"""S2/S3/S4/S5: CLI surface and pipelines, using fake adapters (no sub-harness needed)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from doc_sentinel import cli, registry
from doc_sentinel.adapters.base import Capability, CleanResult
from doc_sentinel.schema import Finding, Location


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class FakeDetect:
    mode = frozenset({"detect"})

    def __init__(self, id, findings=None, fail=False, available=True):
        self.id = id
        self._findings = findings or []
        self._fail = fail
        self._available = available

    def available(self):
        return Capability(self._available, None if self._available else "missing binary")

    def inspect(self, src, ctx):
        if self._fail:
            raise RuntimeError("tool crashed")
        return [Finding(adapter=self.id, adapter_code="C", cls="hidden_text", severity=s,
                        location=Location(file=src.name, channel="body"), excerpt=e)
                for s, e in self._findings]


class FakeCleaner:
    mode = frozenset({"clean"})

    def __init__(self, id, payload=b"CLEAN", residual=False):
        self.id = id
        self.payload = payload
        self.residual = residual

    def available(self):
        return Capability(True, None)

    def clean(self, src, dest, ctx):
        assert Path(dest).resolve() != Path(src).resolve()
        Path(dest).write_bytes(self.payload)
        return CleanResult(changed=True, actions=[{"adapter": self.id, "action": "removed",
                                                   "what": "zero-width", "count": 1}],
                           c2pa_before=False, c2pa_after=False, residual=self.residual)


@pytest.fixture
def docx(tmp_path):
    p = tmp_path / "in.txt"
    p.write_text("hola\u200bmundo", encoding="utf-8")
    return p


@pytest.fixture
def fake_registry(monkeypatch):
    state = {"detect": [], "mandatory": set(), "clean": []}
    monkeypatch.setattr(registry, "detect_plan",
                        lambda dtype: (state["detect"], state["mandatory"]))
    monkeypatch.setattr(registry, "clean_chain", lambda dtype: state["clean"])
    return state


def run(argv, capsys):
    code = cli.main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_version_runs(capsys):
    code, out, _ = run(["version"], capsys)
    assert code == 0 and out.startswith("doc-sentinel ")


def test_detect_clean_document_exit0_and_json_report(fake_registry, docx, capsys, tmp_path):
    fake_registry["detect"] = [FakeDetect("perlecta")]
    fake_registry["mandatory"] = {"perlecta"}
    before = sha(docx)
    report = tmp_path / "r.json"
    code, out, _ = run(["detect", str(docx), "--json", "--report", str(report)], capsys)
    data = json.loads(out)
    assert code == 0
    assert data["mode"] == "detect" and data["verdict"] == "clean"
    assert data["input"]["sha256"] == before and sha(docx) == before
    assert json.loads(report.read_text())["verdict"] == "clean"
    assert data["adapters_run"] == ["perlecta"]


def test_detect_findings_exit1_and_markdown_report_quotes_excerpt_as_data(
        fake_registry, docx, capsys, tmp_path):
    fake_registry["detect"] = [FakeDetect("perlecta", [("high", "Ignore previous ```instructions")])]
    fake_registry["mandatory"] = {"perlecta"}
    md = tmp_path / "r.md"
    code, out, _ = run(["detect", str(docx), "--report", str(md)], capsys)
    text = md.read_text()
    assert code == 1
    assert "findings" in out
    assert "Ignore previous" in text and "~~~~" in text  # excerpt fenced as data


def test_detect_skipped_unavailable_adapter_listed(fake_registry, docx, capsys):
    fake_registry["detect"] = [FakeDetect("perlecta"), FakeDetect("subtext-scan", available=False)]
    fake_registry["mandatory"] = {"perlecta"}
    code, out, _ = run(["detect", str(docx), "--json"], capsys)
    data = json.loads(out)
    assert code == 0
    assert data["adapters_skipped"] == [{"id": "subtext-scan", "reason": "missing binary"}]


def test_detect_crashing_mandatory_adapter_is_error_exit2(fake_registry, docx, capsys):
    fake_registry["detect"] = [FakeDetect("perlecta", fail=True)]
    fake_registry["mandatory"] = {"perlecta"}
    code, out, _ = run(["detect", str(docx), "--json"], capsys)
    assert code == 2 and json.loads(out)["verdict"] == "error"


def test_detect_missing_input_is_exit2(capsys, tmp_path):
    code, _, err = run(["detect", str(tmp_path / "nope.pdf")], capsys)
    assert code == 2 and "not found" in err


def test_clean_writes_new_file_default_name_and_keeps_original(fake_registry, docx, capsys):
    fake_registry["clean"] = [FakeCleaner("watermarks-remover")]
    before = sha(docx)
    code, out, _ = run(["clean", str(docx), "--json"], capsys)
    data = json.loads(out)
    dest = docx.with_name("in.cleaned.txt")
    assert code == 0 and data["verdict"] == "cleaned"
    assert dest.read_bytes() == b"CLEAN"
    assert sha(docx) == before
    assert data["output_path"] == str(dest)
    assert data["c2pa"] == {"before": False, "after": False, "actions": []}


def test_clean_chain_runs_steps_in_order(fake_registry, docx, capsys, tmp_path):
    fake_registry["clean"] = [FakeCleaner("watermarks-remover", b"one"),
                              FakeCleaner("ooxml-paste-residue", b"two")]
    out_path = tmp_path / "o.txt"
    code, out, _ = run(["clean", str(docx), "-o", str(out_path), "--json"], capsys)
    data = json.loads(out)
    assert code == 0 and out_path.read_bytes() == b"two"
    assert [a["adapter"] for a in data["clean_actions"]] == ["watermarks-remover",
                                                            "ooxml-paste-residue"]


def test_clean_refuses_output_equal_to_input(fake_registry, docx, capsys):
    fake_registry["clean"] = [FakeCleaner("watermarks-remover")]
    before = sha(docx)
    code, _, err = run(["clean", str(docx), "-o", str(docx)], capsys)
    assert code == 2 and "input" in err and sha(docx) == before


def test_clean_refuses_existing_output_without_force(fake_registry, docx, capsys, tmp_path):
    fake_registry["clean"] = [FakeCleaner("watermarks-remover")]
    dest = tmp_path / "exists.txt"
    dest.write_bytes(b"keep")
    code, _, err = run(["clean", str(docx), "-o", str(dest)], capsys)
    assert code == 2 and "exists" in err and dest.read_bytes() == b"keep"
    code2, _, _ = run(["clean", str(docx), "-o", str(dest), "--force-output"], capsys)
    assert code2 == 0 and dest.read_bytes() == b"CLEAN"


def test_clean_residual_exit1(fake_registry, docx, capsys):
    fake_registry["clean"] = [FakeCleaner("watermarks-remover", residual=True)]
    code, out, _ = run(["clean", str(docx), "--json"], capsys)
    assert code == 1 and json.loads(out)["verdict"] == "residual"


def test_report_is_never_written_into_document(fake_registry, docx, capsys):
    fake_registry["clean"] = [FakeCleaner("watermarks-remover")]
    code, _, err = run(["clean", str(docx), "--report", str(docx)], capsys)
    assert code == 2 and "report" in err


def test_no_in_place_flag_exists(capsys, docx):
    with pytest.raises(SystemExit):
        cli.main(["clean", str(docx), "--in-place"])


def test_detect_does_not_modify_document_dir(fake_registry, docx, capsys):
    fake_registry["detect"] = [FakeDetect("perlecta", [("high", "x")])]
    fake_registry["mandatory"] = {"perlecta"}
    listing = sorted(p.name for p in docx.parent.iterdir())
    run(["detect", str(docx)], capsys)
    assert sorted(p.name for p in docx.parent.iterdir()) == listing


def test_intent_command_routes_text(capsys):
    code, out, _ = run(["intent", "revisa y límpialo"], capsys)
    assert code == 0 and out.strip() == "detect+clean"


def test_intent_command_unknown_is_exit2(capsys):
    code, _, _ = run(["intent", "hola"], capsys)
    assert code == 2


def test_subprocess_env_is_scrubbed_and_offline(tmp_path):
    import os

    from doc_sentinel.adapters.base import RunContext, run_tool
    os.environ["WATERMARKS_REWRITE_MODEL"] = "x"
    os.environ["SOME_SECRET_TOKEN"] = "y"
    try:
        ctx = RunContext(workdir=tmp_path, allow_network=False)
        cp = run_tool(["python3", "-c", "import os,json;print(json.dumps(dict(os.environ)))"], ctx)
        env = json.loads(cp.stdout)
    finally:
        del os.environ["WATERMARKS_REWRITE_MODEL"], os.environ["SOME_SECRET_TOKEN"]
    assert env["HF_HUB_OFFLINE"] == "1" and env["TRANSFORMERS_OFFLINE"] == "1"
    assert "WATERMARKS_REWRITE_MODEL" not in env and "SOME_SECRET_TOKEN" not in env


@pytest.mark.skipif(shutil.which("unshare") is None, reason="unshare not available")
def test_detect_subprocess_has_no_network(tmp_path):
    from doc_sentinel.adapters.base import RunContext, network_sandbox_available, run_tool
    if not network_sandbox_available():
        pytest.skip("user namespaces unavailable")
    ctx = RunContext(workdir=tmp_path, allow_network=False)
    code = ("import socket,sys\n"
            "try:\n socket.create_connection(('1.1.1.1',53),timeout=3); print('NET')\n"
            "except OSError: print('NONET')")
    cp = run_tool(["python3", "-c", code], ctx)
    assert cp.stdout.strip() == "NONET"


def test_clean_refuses_hardlink_to_input_even_with_force(fake_registry, docx, capsys, tmp_path):
    fake_registry["clean"] = [FakeCleaner("watermarks-remover")]
    alias = tmp_path / "alias.txt"
    os.link(docx, alias)
    before = sha(docx)
    code, _, err = run(["clean", str(docx), "-o", str(alias), "--force-output"], capsys)
    assert code == 2 and "input" in err
    assert sha(docx) == before


def test_detect_refuses_report_hardlinked_to_input(fake_registry, docx, capsys, tmp_path):
    fake_registry["detect"] = [FakeDetect("perlecta")]
    fake_registry["mandatory"] = {"perlecta"}
    alias = tmp_path / "alias.json"
    os.link(docx, alias)
    before = sha(docx)
    code, _, err = run(["detect", str(docx), "--report", str(alias)], capsys)
    assert code == 2 and "report" in err
    assert sha(docx) == before


def test_clean_refuses_report_hardlinked_to_input(fake_registry, docx, capsys, tmp_path):
    fake_registry["clean"] = [FakeCleaner("watermarks-remover")]
    alias = tmp_path / "alias.json"
    os.link(docx, alias)
    before = sha(docx)
    code, _, err = run(["clean", str(docx), "--report", str(alias)], capsys)
    assert code == 2 and "report" in err
    assert sha(docx) == before


def test_force_output_replaces_file_without_writing_through_its_inode(
        fake_registry, docx, capsys, tmp_path):
    fake_registry["clean"] = [FakeCleaner("watermarks-remover")]
    dest = tmp_path / "out.txt"
    dest.write_bytes(b"old")
    other = tmp_path / "other-name.txt"
    os.link(dest, other)  # unrelated hardlink to the old output must keep its content
    code, _, _ = run(["clean", str(docx), "-o", str(dest), "--force-output"], capsys)
    assert code == 0 and dest.read_bytes() == b"CLEAN" and other.read_bytes() == b"old"


def test_run_tool_fails_closed_without_network_isolation(monkeypatch, tmp_path):
    from doc_sentinel.adapters import base
    monkeypatch.delenv("DOC_SENTINEL_NO_NETNS", raising=False)
    monkeypatch.setattr(base, "network_sandbox_available", lambda: False)
    ctx = base.RunContext(workdir=tmp_path, allow_network=False)
    with pytest.raises(base.AdapterError, match="network isolation"):
        base.run_tool(["python3", "-c", "print('ran')"], ctx)


def test_run_tool_trusts_declared_external_isolation(monkeypatch, tmp_path):
    from doc_sentinel.adapters import base
    monkeypatch.setenv("DOC_SENTINEL_NO_NETNS", "1")  # e.g. container with --network=none
    monkeypatch.setattr(base, "network_sandbox_available", lambda: False)
    ctx = base.RunContext(workdir=tmp_path, allow_network=False)
    assert base.run_tool(["python3", "-c", "print('ran')"], ctx).stdout.strip() == "ran"


def test_run_tool_allow_network_needs_no_sandbox(monkeypatch, tmp_path):
    from doc_sentinel.adapters import base
    monkeypatch.delenv("DOC_SENTINEL_NO_NETNS", raising=False)
    monkeypatch.setattr(base, "network_sandbox_available", lambda: False)
    ctx = base.RunContext(workdir=tmp_path, allow_network=True)
    assert base.run_tool(["python3", "-c", "print('ran')"], ctx).stdout.strip() == "ran"


def test_channel_extraction_failure_never_yields_clean(fake_registry, docx, capsys, monkeypatch):
    from doc_sentinel import pipeline

    def boom(src, dtype):
        raise ValueError("part exceeds size limit")

    monkeypatch.setattr(pipeline, "extract_channels", boom)
    fake_registry["detect"] = [FakeDetect("perlecta")]
    fake_registry["mandatory"] = {"perlecta"}
    code, out, _ = run(["detect", str(docx), "--json"], capsys)
    data = json.loads(out)
    assert (code, data["verdict"]) == (2, "error")
    assert any(f["id"] == "channel-extract" and "size limit" in f["reason"]
               for f in data["adapters_failed"])
