"""S12 launcher contract, exercised against a recording Docker executable."""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "doc-sentinel:local"
UPDATE_ERROR = "update runs on a git checkout with network: use the Python CLI on the host (doc-sentinel update)"
NETWORK_ERROR = "--allow-network is not available in the container launcher"
OUTPUT_ERROR = "output path must differ from the input (no in-place cleaning)"
REPORT_CLEAN_ERROR = "report path must differ from the input and the output document"
REPORT_DETECT_ERROR = "report path must differ from the input document"


class Harness:
    def __init__(self, root, backend="sh"):
        self.root = root
        self.backend = backend
        self.repo = root / "repo"
        self.scripts = self.repo / "scripts"
        self.scripts.mkdir(parents=True)
        name = "doc-sentinel-docker.sh" if backend == "sh" else "doc-sentinel.ps1"
        source = ROOT / "scripts" / name
        if source.exists():
            shutil.copyfile(source, self.scripts / name)
        (self.repo / "containers").mkdir()
        (self.repo / "containers" / "Dockerfile").write_text("# build fixture\n")
        (self.repo / "third_party" / "one").mkdir(parents=True)
        (self.repo / "third_party" / "one" / "source").write_text("fixture\n")
        self.bin = root / "bin"
        self.bin.mkdir()
        if backend == "sh":
            # Never fall through to a real Docker CLI, including the missing-Docker case.
            for tool in ("dirname", "basename", "realpath", "uname", "id", "mkdir", "head", "grep"):
                (self.bin / tool).symlink_to(shutil.which(tool))
        fake = ROOT / "tests" / "launchers" / ("fake-docker.py" if backend == "sh" else "fake-docker.ps1")
        shutil.copyfile(fake, self.bin / "docker")
        (self.bin / "docker").chmod(0o755)
        fake_git = ROOT / "tests" / "launchers" / ("fake-git.py" if backend == "sh" else "fake-git.ps1")
        shutil.copyfile(fake_git, self.bin / "git")
        (self.bin / "git").chmod(0o755)
        self.input_dir = root / "input docs"
        self.output_dir = root / "output docs"
        self.report_dir = root / "reports here"
        for directory in (self.input_dir, self.output_dir, self.report_dir):
            directory.mkdir()
        self.input = self.input_dir / "informe final.docx"
        self.input.write_bytes(b"synthetic input, unchanged")
        self.log = root / "calls.jsonl"
        self.config = {}

    def path(self, path):
        path = Path(path)
        if self.backend == "pwsh":
            return str(Path("/case") / path.relative_to(self.root))
        return str(path)

    def run(self, *args):
        env = {"PATH": str(self.bin), "LANG": "C.UTF-8",
               "FAKE_LOG": str(self.log), **self.config}
        args = [self.path(a) if isinstance(a, Path) else str(a) for a in args]
        if self.backend == "sh":
            cmd = ["/bin/sh", str(self.scripts / "doc-sentinel-docker.sh"), *args]
        else:
            pytest.fail("PowerShell harness must override run")
        return subprocess.run(cmd, cwd=self.root, env=env, text=True, capture_output=True, check=False)

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8-sig").splitlines()]

    def calls_named(self, name):
        return [call["args"] for call in self.calls() if call["args"][:1] == [name]]

    def run_args(self):
        calls = self.calls_named("run")
        assert len(calls) == 1
        return calls[0]

    def assert_base(self, run, mounts):
        assert run[:9] == ["run", "--rm", "--network=none", "--read-only", "--tmpfs",
                           "/tmp:rw,nosuid,nodev,size=1g", "--cap-drop=ALL",
                           "--security-opt=no-new-privileges", "-e"]
        assert run[9] == "DOC_SENTINEL_NO_NETNS=1"
        if self.backend == "pwsh" or platform.system() == "Linux":
            assert run[10:12] == ["--user", f"{os.getuid()}:{os.getgid()}" if self.backend == "sh" else "0:0"]
            pos = 12
        else:
            pos = 10
        while run[pos:pos + 1] == ["-e"]:
            assert run[pos + 1].startswith("DOC_SENTINEL_HOST_")
            pos += 2
        for host_dir, target, mode in mounts:
            assert run[pos:pos + 2] == ["-v", f"{self.path(host_dir)}:{target}:{mode}"]
            pos += 2
        assert run[pos] == IMAGE
        return run[pos + 1:]


@pytest.fixture
def harness():
    with tempfile.TemporaryDirectory(prefix="doc-sentinel-launchers-") as directory:
        yield Harness(Path(directory) / "case with spaces")


class LauncherScenarios:
    def test_clean_default_output_and_paths_with_spaces(self, harness):
        result = harness.run("clean", harness.input, "--json")
        expected = harness.input_dir / "informe final.cleaned.docx"
        assert (result.returncode, result.stdout, result.stderr) == (
            0, "", f"Cleaned copy: {harness.path(expected)}\n")
        args = harness.assert_base(harness.run_args(), [
            (harness.input_dir, "/work/in", "ro"), (harness.input_dir, "/work/out", "rw")])
        assert args == ["clean", "/work/in/informe final.docx", "--output-dir", "/work/out", "--json"]
        assert harness.input.read_bytes() == b"synthetic input, unchanged"

    @pytest.mark.parametrize("name", [
        "quarterly.report.v1.docx", ".hidden", ".hidden.txt", "..hidden", "name..txt", "trailing.", "noext",
    ])
    def test_clean_default_note_uses_cli_default_name(self, harness, name):
        src = harness.input_dir / name
        src.write_bytes(b"synthetic input, unchanged")
        expected = harness.input_dir / f"{src.stem}.cleaned{src.suffix}"
        harness.config.update(FAKE_RUN_STDOUT="output\n", FAKE_RUN_STDERR="detail\n")
        result = harness.run("clean", src)
        assert (result.returncode, result.stdout, result.stderr) == (
            0, "output\n", f"detail\nCleaned copy: {harness.path(expected)}\n")
        assert src.read_bytes() == b"synthetic input, unchanged"
        assert harness.run_args()[-2:] == ["--output-dir", "/work/out"]

    def test_clean_default_note_uses_resolved_input_name(self, harness):
        alias = harness.input_dir / "alias.txt"
        alias.symlink_to(harness.input.name)
        expected = harness.input_dir / "informe final.cleaned.docx"
        result = harness.run("clean", alias)
        assert (result.returncode, result.stdout, result.stderr) == (
            0, "", f"Cleaned copy: {harness.path(expected)}\n")
        assert harness.input.read_bytes() == b"synthetic input, unchanged"
        assert harness.run_args()[-3:] == ["/work/in/informe final.docx", "--output-dir", "/work/out"]

    @pytest.mark.parametrize("elsewhere,relative", [(False, True), (True, False), (True, True)])
    def test_clean_default_output_symlink_uses_resolved_target(self, harness, elsewhere, relative):
        directory = harness.output_dir if elsewhere else harness.input_dir
        target = directory / "actual clean copy.docx"
        target.write_bytes(b"existing copy")
        alias = harness.input_dir / "informe final.cleaned.docx"
        link = (Path("..") / directory.name / target.name if elsewhere else Path(target.name))
        alias.symlink_to(link if relative else harness.path(target))
        result = harness.run("clean", harness.input, "--force-output", "--agent-summary")
        assert (result.returncode, result.stdout, result.stderr) == (
            0, "", f"Cleaned copy: {harness.path(target)}\n")
        run = harness.run_args()
        assert f"DOC_SENTINEL_HOST_OUTPUT={harness.path(target)}" in run
        args = harness.assert_base(run, [
            (harness.input_dir, "/work/in", "ro"), (directory, "/work/out", "rw")])
        assert args == ["clean", "/work/in/informe final.docx", "-o", "/work/out/actual clean copy.docx",
                        "--force-output", "--agent-summary"]
        assert harness.input.read_bytes() == b"synthetic input, unchanged"
        assert target.read_bytes() == b"existing copy" and alias.is_symlink()

    @pytest.mark.parametrize("conflict", ["input", "report"])
    def test_clean_default_output_symlink_refuses_protected_target(self, harness, conflict):
        target = harness.input if conflict == "input" else harness.output_dir / "target.docx"
        if conflict == "report":
            target.write_bytes(b"existing copy")
        before = target.read_bytes()
        (harness.input_dir / "informe final.cleaned.docx").symlink_to(
            target.name if conflict == "input" else Path("..") / harness.output_dir.name / target.name)
        args = ["clean", harness.input, "--force-output"]
        if conflict == "report":
            args += ["--report", target]
        result = harness.run(*args)
        error = OUTPUT_ERROR if conflict == "input" else REPORT_CLEAN_ERROR
        assert (result.returncode, result.stdout, result.stderr) == (2, "", error + "\n")
        assert target.read_bytes() == before and harness.input.read_bytes() == b"synthetic input, unchanged"
        assert harness.calls() == []

    def test_clean_custom_output_and_separate_report(self, harness):
        output = harness.output_dir / "clean copy.docx"
        report = harness.report_dir / "audit report.json"
        result = harness.run("clean", harness.input, "--output", output, "--force-output",
                             "--json", "--report", report)
        assert (result.returncode, result.stdout, result.stderr) == (
            0, "", f"Cleaned copy: {harness.path(output)}\n")
        args = harness.assert_base(harness.run_args(), [
            (harness.input_dir, "/work/in", "ro"), (harness.output_dir, "/work/out", "rw"),
            (harness.report_dir, "/work/report", "rw")])
        assert args == ["clean", "/work/in/informe final.docx", "-o", "/work/out/clean copy.docx",
                        "--force-output", "--json", "--report", "/work/report/audit report.json"]

    def test_clean_report_reuses_output_mount(self, harness):
        output = harness.output_dir / "copy.docx"
        report = harness.output_dir / "report.json"
        result = harness.run("clean", harness.input, "-o", output, "--report", report)
        assert result.returncode == 0
        args = harness.assert_base(harness.run_args(), [
            (harness.input_dir, "/work/in", "ro"), (harness.output_dir, "/work/out", "rw")])
        assert args == ["clean", "/work/in/informe final.docx", "-o", "/work/out/copy.docx",
                        "--report", "/work/out/report.json"]

    def test_detect_with_all_supported_options(self, harness):
        report = harness.report_dir / "report.json"
        result = harness.run("detect", harness.input, "--json", "--report", report,
                             "--fail-on", "medium", "--prompt-guard")
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
        args = harness.assert_base(harness.run_args(), [
            (harness.input_dir, "/work/in", "ro"), (harness.report_dir, "/work/report", "rw")])
        assert args == ["detect", "/work/in/informe final.docx", "--json", "--report",
                        "/work/report/report.json", "--fail-on", "medium", "--prompt-guard"]

    def test_detect_review_dir_and_agent_summary(self, harness):
        review = harness.report_dir / "review dir"
        review.mkdir()
        report = harness.report_dir / "report.json"
        result = harness.run("detect", harness.input, "--agent-summary", "--review-dir", review,
                             "--report", report)
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
        recorded = harness.run_args()
        assert f"DOC_SENTINEL_HOST_REVIEW={harness.path(review)}" in recorded
        assert f"DOC_SENTINEL_HOST_INPUT={harness.path(harness.input)}" in recorded
        args = harness.assert_base(recorded, [
            (harness.input_dir, "/work/in", "ro"), (harness.report_dir, "/work/report", "rw"),
            (review, "/work/review", "rw")])
        assert args == ["detect", "/work/in/informe final.docx", "--agent-summary", "--report",
                        "/work/report/report.json", "--review-dir", "/work/review"]
        assert harness.input.read_bytes() == b"synthetic input, unchanged"

    def test_review_merge_maps_review_and_report(self, harness):
        review = harness.report_dir / "review dir"
        review.mkdir()
        report = harness.report_dir / "merged.json"
        result = harness.run("review-merge", "--review-dir", review, "--report", report,
                             "--agent-summary")
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
        recorded = harness.run_args()
        assert f"DOC_SENTINEL_HOST_REVIEW={harness.path(review)}" in recorded
        assert f"DOC_SENTINEL_HOST_REPORT={harness.path(report)}" in recorded
        args = harness.assert_base(recorded, [
            (harness.report_dir, "/work/report", "rw"), (review, "/work/review", "rw")])
        assert args == ["review-merge", "--agent-summary", "--report", "/work/report/merged.json",
                        "--review-dir", "/work/review"]

    @pytest.mark.parametrize("mode", ["detect", "sanitize"])
    def test_sanitize_maps_original_read_only_and_output_review_mounts(self, harness, mode):
        review = harness.report_dir / "review dir"
        review.mkdir()
        output = harness.output_dir / "safe copy.docx"
        report = harness.report_dir / "audit.json"
        args = [mode, harness.input, "--review-dir", review, "-o", output,
                "--force-output", "--report", report, "--agent-summary"]
        if mode == "detect":
            args.append("--sanitize")
        result = harness.run(*args)
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
        recorded = harness.run_args()
        assert f"DOC_SENTINEL_HOST_INPUT={harness.path(harness.input)}" in recorded
        assert f"DOC_SENTINEL_HOST_OUTPUT={harness.path(output)}" in recorded
        forwarded = harness.assert_base(recorded, [
            (harness.input_dir, "/work/in", "ro"), (harness.output_dir, "/work/out", "rw"),
            (harness.report_dir, "/work/report", "rw"), (review, "/work/review", "rw")])
        expected = [mode, "/work/in/informe final.docx", "-o", "/work/out/safe copy.docx",
                    "--force-output", "--agent-summary", "--report", "/work/report/audit.json",
                    "--review-dir", "/work/review"]
        if mode == "detect":
            expected.append("--sanitize")
        assert forwarded == expected
        assert harness.input.read_bytes() == b"synthetic input, unchanged"

    @pytest.mark.parametrize("mode", ["detect", "sanitize"])
    @pytest.mark.parametrize("suffix", [".pdf", ".PDF"])
    def test_pdf_default_sanitized_output_is_docx_on_host(self, harness, mode, suffix):
        src = harness.input_dir / f"source file{suffix}"
        src.write_bytes(b"%PDF-1.7\nsynthetic PDF input")
        review = harness.report_dir / "review"
        review.mkdir()
        args = [mode, src, "--agent-summary"]
        args += ["--sanitize"] if mode == "detect" else ["--review-dir", review]
        result = harness.run(*args)
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
        recorded = harness.run_args()
        assert f"DOC_SENTINEL_HOST_OUTPUT_DIR={harness.path(harness.input_dir)}" in recorded
        assert "--output-dir" in recorded and "/work/out" in recorded
        assert src.read_bytes() == b"%PDF-1.7\nsynthetic PDF input"

    @pytest.mark.parametrize("mode", ["detect", "sanitize"])
    @pytest.mark.parametrize("name,data,output", [
        ("source", b"%PDF-1.7\nsynthetic", "source.sanitized.docx"),
        ("source.bin", b"%PDF-1.7\nsynthetic", "source.sanitized.docx"),
        ("source.pdf", b"ordinary synthetic text", "source.sanitized.pdf"),
    ])
    def test_sanitize_default_type_comes_from_content(self, harness, mode, name, data, output):
        src = harness.input_dir / name
        src.write_bytes(data)
        review = harness.report_dir / "review"
        review.mkdir()
        args = [mode, src, "--agent-summary"]
        args += ["--sanitize"] if mode == "detect" else ["--review-dir", review]
        result = harness.run(*args)
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
        recorded = harness.run_args()
        assert f"DOC_SENTINEL_HOST_OUTPUT_DIR={harness.path(harness.input_dir)}" in recorded
        assert "--output-dir" in recorded and "/work/out" in recorded and src.read_bytes() == data

    def test_clean_agent_summary_is_forwarded(self, harness):
        result = harness.run("clean", harness.input, "--agent-summary")
        assert result.returncode == 0
        args = harness.assert_base(harness.run_args(), [
            (harness.input_dir, "/work/in", "ro"), (harness.input_dir, "/work/out", "rw")])
        assert args == ["clean", "/work/in/informe final.docx", "--output-dir", "/work/out", "--agent-summary"]

    @pytest.mark.parametrize("mode", ["clean", "detect", "sanitize"])
    def test_custom_output_dir_is_mounted_and_passed_to_cli(self, harness, mode):
        review = harness.report_dir / "review"
        review.mkdir()
        args = [mode, harness.input, "--output-dir", harness.output_dir, "--agent-summary"]
        if mode == "detect":
            args.append("--sanitize")
        elif mode == "sanitize":
            args.extend(["--review-dir", review])
        result = harness.run(*args)
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
        recorded = harness.run_args()
        assert f"DOC_SENTINEL_HOST_OUTPUT_DIR={harness.path(harness.output_dir)}" in recorded
        assert "--output-dir" in recorded and "/work/out" in recorded
        assert f"{harness.path(harness.output_dir)}:/work/out:rw" in recorded
        assert harness.input.read_bytes() == b"synthetic input, unchanged"

    def test_output_dir_and_output_are_mutually_exclusive(self, harness):
        output = harness.output_dir / "output.docx"
        result = harness.run("clean", harness.input, "-o", output, "--output-dir", harness.output_dir)
        assert result.returncode == 2 and result.stdout == "" and "mutually exclusive" in result.stderr
        assert not output.exists() and not harness.calls()

    @pytest.mark.parametrize("ancestor", [False, True])
    def test_refuse_review_dir_containing_input(self, harness, ancestor):
        review = harness.root if ancestor else harness.input_dir
        result = harness.run("detect", harness.input, "--review-dir", review, "--agent-summary")
        assert result.returncode == 2 and result.stdout == "" and "review" in result.stderr.lower()
        assert harness.input.read_bytes() == b"synthetic input, unchanged"
        assert harness.calls() == []

    @pytest.mark.parametrize("args", [("version",), ("intent", "revisa y límpialo")])
    def test_version_and_intent_pass_through(self, harness, args):
        harness.config.update(FAKE_RUN_STDOUT="answer\n", FAKE_RUN_STDERR="detail\n")
        result = harness.run(*args)
        assert (result.returncode, result.stdout, result.stderr) == (0, "answer\n", "detail\n")
        assert harness.assert_base(harness.run_args(), []) == list(args)

    def test_nonzero_exit_and_streams_are_unchanged(self, harness):
        harness.config.update(FAKE_RUN_CODE="1", FAKE_RUN_STDOUT="findings\n", FAKE_RUN_STDERR="warning\n")
        result = harness.run("detect", harness.input)
        assert (result.returncode, result.stdout, result.stderr) == (1, "findings\n", "warning\n")
        assert harness.calls_named("run")

    def test_passthrough_without_trailing_newline(self, harness):
        harness.config.update(FAKE_RUN_CODE="1", FAKE_RUN_STDOUT="no newline", FAKE_RUN_STDERR="warning")
        result = harness.run("detect", harness.input)
        assert (result.returncode, result.stdout, result.stderr) == (1, "no newline", "warning")
        assert harness.calls_named("run")

    def test_clean_nonzero_has_no_success_note(self, harness):
        harness.config.update(FAKE_RUN_CODE="1", FAKE_RUN_STDOUT="residual\n", FAKE_RUN_STDERR="detail\n")
        result = harness.run("clean", harness.input)
        assert (result.returncode, result.stdout, result.stderr) == (1, "residual\n", "detail\n")

    @pytest.mark.parametrize("case", ["update", "network", "output_input", "report_input_clean",
                                      "report_input_detect", "report_output", "missing_input"])
    def test_refusals_do_not_invoke_docker_or_modify_input(self, harness, case):
        output = harness.output_dir / "output.docx"
        args, message = {
            "update": (["update"], UPDATE_ERROR),
            "network": (["detect", harness.input, "--allow-network"], NETWORK_ERROR),
            "output_input": (["clean", harness.input, "-o", harness.input], OUTPUT_ERROR),
            "report_input_clean": (["clean", harness.input, "--report", harness.input], REPORT_CLEAN_ERROR),
            "report_input_detect": (["detect", harness.input, "--report", harness.input], REPORT_DETECT_ERROR),
            "report_output": (["clean", harness.input, "-o", output, "--report", output], REPORT_CLEAN_ERROR),
            "missing_input": (["detect", harness.input_dir / "missing.docx"],
                              f"input not found: {harness.path(harness.input_dir / 'missing.docx')}"),
        }[case]
        before = harness.input.read_bytes()
        result = harness.run(*args)
        assert (result.returncode, result.stdout, result.stderr) == (2, "", message + "\n")
        assert harness.input.read_bytes() == before
        assert not output.exists()
        assert harness.calls() == []

    def test_refuse_aliased_input(self, harness):
        alias = harness.input_dir / "alias.docx"
        alias.symlink_to(harness.input.name)
        result = harness.run("clean", harness.input, "-o", alias)
        assert (result.returncode, result.stdout, result.stderr) == (2, "", OUTPUT_ERROR + "\n")
        assert harness.input.read_bytes() == b"synthetic input, unchanged"
        assert harness.calls() == []

    def test_missing_docker(self, harness):
        (harness.bin / "docker").unlink()
        result = harness.run("version")
        assert (result.returncode, result.stdout, result.stderr) == (
            2, "", "docker is not installed or not on PATH\n")
        assert harness.calls() == []

    def test_daemon_down(self, harness):
        harness.config["FAKE_INFO_CODE"] = "1"
        result = harness.run("version")
        assert (result.returncode, result.stdout, result.stderr) == (
            2, "", "Docker daemon is not available\n")
        assert len(harness.calls_named("info")) == 1
        assert harness.calls_named("run") == []

    def test_missing_image_builds_from_repo(self, harness):
        harness.config["FAKE_IMAGE_CODE"] = "1"
        result = harness.run("version")
        assert (result.returncode, result.stdout) == (0, "")
        assert "build stdout\n" in result.stderr and "build stderr\n" in result.stderr
        assert harness.calls_named("build") == [["build", "-f", harness.path(harness.repo / "containers" / "Dockerfile"),
                                                  "-t", IMAGE, harness.path(harness.repo)]]
        assert harness.calls_named("run")

    def stamp(self, harness):
        return harness.repo / ".cache" / "doc-sentinel-image"

    def test_image_from_another_commit_is_rebuilt_and_recorded(self, harness):
        self.stamp(harness).parent.mkdir()
        self.stamp(harness).write_text("oldcommit sha256:fake")
        harness.config.update(FAKE_GIT_HEAD="newcommit", FAKE_IMAGE_ID="sha256:built")
        result = harness.run("version")
        assert result.returncode == 0
        assert len(harness.calls_named("build")) == 1 and harness.calls_named("run")
        assert self.stamp(harness).read_text().strip() == "newcommit sha256:built"

    def test_image_recorded_for_current_commit_is_reused(self, harness):
        self.stamp(harness).parent.mkdir()
        self.stamp(harness).write_text("samecommit sha256:fake")
        harness.config.update(FAKE_GIT_HEAD="samecommit")
        result = harness.run("version")
        assert result.returncode == 0 and not result.stderr
        assert harness.calls_named("build") == [] and harness.calls_named("run")

    def test_image_replaced_since_recording_is_rebuilt(self, harness):
        self.stamp(harness).parent.mkdir()
        self.stamp(harness).write_text("samecommit sha256:other")
        harness.config.update(FAKE_GIT_HEAD="samecommit")
        assert harness.run("version").returncode == 0
        assert len(harness.calls_named("build")) == 1

    def test_without_git_existing_image_is_reused(self, harness):
        result = harness.run("version")
        assert result.returncode == 0
        assert harness.calls_named("build") == [] and harness.calls_named("run")

    def test_enclosing_repository_is_not_mistaken_for_the_scanner(self, harness):
        harness.config.update(FAKE_GIT_HEAD="parentcommit", FAKE_GIT_TOPLEVEL="/somewhere/else",
                              FAKE_GIT_DIRTY=" M notes.txt")
        result = harness.run("version")
        assert result.returncode == 0
        assert harness.calls_named("build") == [] and not self.stamp(harness).exists()

    def test_uncommitted_changes_always_rebuild_without_recording(self, harness):
        self.stamp(harness).parent.mkdir()
        self.stamp(harness).write_text("samecommit sha256:fake")
        harness.config.update(FAKE_GIT_HEAD="samecommit", FAKE_GIT_DIRTY=" M src/doc_sentinel/cli.py")
        assert harness.run("version").returncode == 0
        assert len(harness.calls_named("build")) == 1
        assert self.stamp(harness).read_text() == ""

    def test_missing_submodule_refuses_build(self, harness):
        (harness.repo / "third_party" / "one" / "source").unlink()
        harness.config["FAKE_IMAGE_CODE"] = "1"
        result = harness.run("version")
        assert (result.returncode, result.stdout, result.stderr) == (
            2, "", "run: git submodule update --init --recursive\n")
        assert harness.calls_named("build") == [] and harness.calls_named("run") == []

    def test_build_failure_is_propagated_without_running(self, harness):
        harness.config.update(FAKE_IMAGE_CODE="1", FAKE_BUILD_CODE="7")
        result = harness.run("version")
        assert result.returncode == 7 and result.stdout == ""
        assert len(harness.calls_named("build")) == 2
        assert "build stdout\n" in result.stderr and "build stderr\n" in result.stderr
        assert not harness.calls_named("run")

    def test_help_and_no_args(self, harness):
        help_result = harness.run("--help")
        assert help_result.returncode == 0 and "usage:" in help_result.stdout.lower() and not help_result.stderr
        no_args = harness.run()
        assert no_args.returncode == 2 and "usage:" in no_args.stdout.lower() and not no_args.stderr
        assert not harness.calls()

    @pytest.mark.parametrize("args", [("detect", "--bogus"), ("clean",),
                                      ("detect", "--fail-on", "critical")])
    def test_bad_arguments_are_rejected(self, harness, args):
        result = harness.run(*args)
        assert result.returncode == 2 and result.stdout == "" and result.stderr
        assert not harness.calls_named("run")


class TestShLauncher(LauncherScenarios):
    def test_review_root_is_refused(self, harness):
        result = harness.run("detect", harness.input, "--review-dir", "/", "--agent-summary")
        assert result.returncode == 2 and result.stdout == "" and "review" in result.stderr.lower()
        assert not harness.calls() and harness.input.read_bytes() == b"synthetic input, unchanged"

    def test_summary_retains_user_input_path_not_symlink_target(self, harness):
        alias = harness.input_dir / "alias.txt"
        alias.symlink_to(harness.input.name)
        result = harness.run("detect", alias, "--agent-summary")
        assert result.returncode == 0
        assert f"DOC_SENTINEL_HOST_INPUT={harness.path(alias)}" in harness.run_args()

    def test_rebuild_switch(self, harness):
        result = harness.run("--rebuild", "version")
        assert result.returncode == 0
        assert len(harness.calls_named("build")) == 1
