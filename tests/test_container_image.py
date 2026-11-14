"""Exercise the unified image as an unprivileged, offline, read-only CLI."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "doc-sentinel:local"
USER = "10001:10001"
pytestmark = pytest.mark.integration


@pytest.fixture(scope="session", autouse=True)
def image_available():
    if shutil.which("docker") is None:
        pytest.skip("docker is unavailable")
    inspected = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True,
                               text=True, check=False)
    if inspected.returncode:
        pytest.skip(f"docker image {IMAGE} is unavailable")
    return json.loads(inspected.stdout)[0]["Architecture"]


@pytest.fixture
def mounts(corpus_directory):
    # Docker's fixed UID need not equal the host user. These directories contain
    # generated synthetic documents only; no repository-root files are mounted.
    with tempfile.TemporaryDirectory(prefix="doc-sentinel-container-") as scratch:
        root = Path(scratch)
        source, output, report = (root / name for name in ("in", "out", "report"))
        for directory in (source, output, report):
            directory.mkdir(mode=0o777)
            directory.chmod(0o777)
        for name in ("exam-hidden-ai.docx", "exam-hidden-ai.pdf", "spanish-exam-control.docx",
                     "ai-traces.docx", "ai-traces.xlsx", "ai-traces.pptx",
                     "pdf-ai-producer-traces.pdf", "c2pa-signed-C.jpg"):
            fixture = corpus_directory / name
            if fixture.is_file():
                shutil.copyfile(fixture, source / name)
                (source / name).chmod(0o644)
        (source / "zero-width.txt").write_text("hola\u200bmundo\n", encoding="utf-8")
        (source / "zero-width.txt").chmod(0o644)
        yield source, output, report


def docker_run(args, mounts=None, *, entrypoint=None, isolated=True, external_netns=True):
    command = ["docker", "run", "--rm"]
    if isolated:
        command.append("--network=none")
    command += ["--read-only", "--tmpfs", "/tmp", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--user", USER]
    if external_netns:
        command += ["-e", "DOC_SENTINEL_NO_NETNS=1"]
    if mounts is not None:
        source, output, report = mounts
        command += ["-v", f"{source}:/work/in:ro", "-v", f"{output}:/work/out:rw",
                    "-v", f"{report}:/work/report:rw"]
    if entrypoint:
        command += ["--entrypoint", entrypoint]
    command += [IMAGE, *args]
    return subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_report(proc):
    assert proc.stdout, f"exit={proc.returncode}; stderr={proc.stderr}"
    report = json.loads(proc.stdout)
    assert proc.stderr.startswith(f"doc-sentinel {report['mode']}: verdict={report['verdict']}\n")
    return report


def read_container_report(mounts, filename):
    # The CLI deliberately creates private 0600 files owned by UID 10001;
    # a different host UID must not be expected to read them directly.
    proc = docker_run(["-c", "import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text())",
                       f"/work/report/{filename}"], mounts, entrypoint="python")
    assert proc.returncode == 0 and proc.stderr == "", proc.stderr
    return json.loads(proc.stdout)


def test_version_lists_all_pins_without_git_directory():
    proc = docker_run(["version"])
    assert proc.returncode == 0 and proc.stderr == ""
    assert proc.stdout.startswith("doc-sentinel ")
    lock = json.loads((ROOT / "pins.lock.json").read_text(encoding="utf-8"))
    for pin in lock["pins"]:
        assert pin["id"] in proc.stdout and pin["sha"][:12] in proc.stdout
    verify = docker_run(["-c", "test ! -e /opt/doc-sentinel/.git"], entrypoint="sh")
    assert (verify.returncode, verify.stdout, verify.stderr) == (0, "", "")


@pytest.mark.parametrize("name,expected", [
    ("exam-hidden-ai.docx", ("findings", 1)),
    ("exam-hidden-ai.pdf", ("findings", 1)),
    ("spanish-exam-control.docx", ("clean", 0)),
    ("zero-width.txt", ("findings", 0)),
])
def test_detect_matches_host_without_skipped_adapters(mounts, name, expected):
    source, _, reports = mounts
    original = source / name
    if not original.exists() and name == "exam-hidden-ai.pdf":
        pytest.skip("LibreOffice is unavailable for generating the exam PDF")
    assert original.exists()
    before = digest(original)
    host = subprocess.run([sys.executable, "-m", "doc_sentinel.cli", "detect",
                           str(original), "--json"], capture_output=True, text=True,
                          timeout=300, check=False)
    host_report = parse_report(host)
    container = docker_run(["detect", f"/work/in/{name}", "--json", "--report",
                            "/work/report/detect.json"], mounts)
    report = parse_report(container)
    assert (host_report["verdict"], host.returncode) == expected, host_report
    assert (report["verdict"], container.returncode) == expected, report
    assert report["adapters_failed"] == [] and report["adapters_skipped"] == [], report
    assert host_report["adapters_failed"] == [] and host_report["adapters_skipped"] == [], host_report
    assert report["input"]["sha256"] == before == digest(original)
    assert read_container_report(mounts, "detect.json") == report
    assert (reports / "detect.json").stat().st_uid == 10001
    assert (reports / "detect.json").stat().st_mode & 0o777 == 0o600
    print(f"{name}: host={host_report['verdict']}/{host.returncode}, "
          f"container={report['verdict']}/{container.returncode}; "
          f"failed={report['adapters_failed']}, skipped={report['adapters_skipped']}")


@pytest.mark.parametrize("name", ["ai-traces.docx", "ai-traces.xlsx", "ai-traces.pptx",
                                  "pdf-ai-producer-traces.pdf", "c2pa-signed-C.jpg"])
def test_clean_writes_owned_copy_and_report_with_tools_available(mounts, name, image_available):
    source, output, reports = mounts
    original = source / name
    if not original.exists() and name == "c2pa-signed-C.jpg":
        pytest.skip("the pinned public signed C2PA JPEG is not cached")
    assert original.exists()
    before = digest(original)
    if name == "c2pa-signed-C.jpg" and image_available == "amd64":
        inspection = docker_run([f"/work/in/{name}"], mounts, entrypoint="c2patool")
        assert inspection.returncode == 0, inspection.stderr
        assert json.loads(inspection.stdout).get("active_manifest"), inspection.stdout
    proc = docker_run(["clean", f"/work/in/{name}", "-o", f"/work/out/{name}",
                       "--json", "--report", "/work/report/clean.json"], mounts)
    report = parse_report(proc)
    assert (report["verdict"], proc.returncode) == ("cleaned", 0), report
    assert report["adapters_failed"] == [] and report["adapters_skipped"] == [], report
    unavailable = [warning for warning in report["warnings"] if "unavailable" in warning.lower()]
    if image_available == "arm64":
        assert unavailable == ["c2patool unavailable: C2PA inspection cannot verify all manifests"]
    else:
        assert unavailable == [], report
    assert report["input"]["sha256"] == before == digest(original)
    assert report["output_path"] == f"/work/out/{name}"
    assert (output / name).is_file() and (output / name).stat().st_uid == 10001
    assert (output / name).stat().st_mode & 0o777 == 0o600
    assert read_container_report(mounts, "clean.json") == report
    assert (reports / "clean.json").stat().st_uid == 10001
    assert (reports / "clean.json").stat().st_mode & 0o777 == 0o600
    if name == "c2pa-signed-C.jpg":
        assert report["c2pa"]["before"] is True and report["c2pa"]["after"] is False


def test_tools_and_runtime_settings(image_available):
    script = ("import json,os,shutil; print(json.dumps({"
              "'exiftool':shutil.which('exiftool'),'c2patool':shutil.which('c2patool'),"
              "'home':os.environ.get('HOME'),'tmp':os.environ.get('TMPDIR'),"
              "'npm':os.environ.get('NPM_CONFIG_CACHE'),'xdg':os.environ.get('XDG_CACHE_HOME'),"
              "'no_netns':os.environ.get('DOC_SENTINEL_NO_NETNS')}))")
    proc = docker_run(["-c", script], entrypoint="python", external_netns=False)
    assert proc.returncode == 0 and proc.stderr == ""
    settings = json.loads(proc.stdout)
    expected_c2patool = None if image_available == "arm64" else "/usr/local/bin/c2patool"
    assert settings == {"exiftool": "/usr/bin/exiftool", "c2patool": expected_c2patool,
                        "home": "/tmp", "tmp": "/tmp", "npm": "/tmp/.npm",
                        "xdg": "/tmp/.cache", "no_netns": None}


def test_c2patool_executes(image_available):
    if image_available == "arm64":
        pytest.skip("upstream c2patool 0.27.23 has no Linux arm64 release")
    version = docker_run(["--version"], entrypoint="c2patool")
    assert (version.returncode, version.stdout.strip(), version.stderr) == (0, "c2patool 0.27.23", "")


def test_no_network_and_only_loopback():
    script = ("import json,os,socket\n"
              "try:\n socket.create_connection(('192.0.2.1',80),timeout=2); connected=True\n"
              "except OSError:\n connected=False\n"
              "print(json.dumps({'connected':connected,'interfaces':os.listdir('/sys/class/net')}))")
    proc = docker_run(["-c", script], entrypoint="python")
    assert proc.returncode == 0 and proc.stderr == ""
    assert json.loads(proc.stdout) == {"connected": False, "interfaces": ["lo"]}


def test_no_personal_documents_in_image():
    script = ("find /opt/doc-sentinel -xdev -type f "
              "\\( -name '*.docx' -o -name '*.xlsx' -o -name '*.pptx' -o -name '*.pdf' "
              "-o -name '*.doc' -o -name '*.odt' \\) -print; "
              "test ! -e /opt/doc-sentinel/samples && "
              "! find /opt/doc-sentinel -maxdepth 1 -iname 'ejemplo*' | grep .")
    proc = docker_run(["-c", script], entrypoint="sh")
    assert proc.returncode == 0 and proc.stderr == "", (proc.stdout, proc.stderr)
    # This image excludes even third-party document fixtures from its source tree.
    assert proc.stdout == "", proc.stdout
    print("Third-party test fixtures in image: none")


@pytest.fixture
def unisolated_detection(mounts):
    original = mounts[0] / "zero-width.txt"
    before = digest(original)
    proc = docker_run(["detect", "/work/in/zero-width.txt", "--json"], mounts, isolated=False)
    return proc, parse_report(proc), before, digest(original)


def test_unisolated_detection_preserves_input_and_reports_errors(unisolated_detection):
    _, report, before, after = unisolated_detection
    assert before == after == report["input"]["sha256"]
    if report["verdict"] == "error":
        assert report["adapters_failed"] or report["adapters_skipped"], report


def test_false_external_isolation_declaration_fails_closed(unisolated_detection):
    proc, report, _, _ = unisolated_detection
    assert (proc.returncode, report["verdict"]) == (2, "error"), report
