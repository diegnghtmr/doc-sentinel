"""Public-CLI corpus gate: real adapters, generated fixtures and explicit gaps."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
import yaml
from conftest import GENERATOR

from doc_sentinel import cli, registry
from doc_sentinel.schema import SEVERITY_RANK

MANIFEST = Path(__file__).parent / "corpus" / "manifest.yaml"
EXTERNAL = GENERATOR.parent / "generated" / "external"
ENTRIES = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["fixtures"]
DETECT = [item for item in ENTRIES if item["mode"] == "detect"]
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

pytestmark = pytest.mark.integration


def _cases(items):
    """Keep demonstrated gaps strict: a future fix must remove its marker."""
    cases = []
    for item in items:
        missing = item.get("known_gap_when_missing")
        gap = item.get("known_gap") and (not missing or not shutil.which(missing))
        marks = pytest.mark.xfail(reason=item["known_gap"], strict=True) if gap else ()
        cases.append(pytest.param(item, id=item["file"], marks=marks))
    return cases


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="session")
def corpus_run(corpus_directory):
    # Both detection tests share one real CLI invocation per fixture.
    results = {}

    def run(item):
        name = item["file"]
        if name not in results:
            src = corpus_directory / name
            if item.get("optional_binary") and not shutil.which(item["optional_binary"]):
                pytest.skip(f"fixture requires {item['optional_binary']} for conversion")
            source = item.get("external_source")
            if source and not (EXTERNAL / source).is_file():
                pytest.skip(f"public signed C2PA fixture unavailable: {source}; run fetch_public.py")
            original = _sha(src)
            args = [item["mode"], str(src), "--json"]
            output = corpus_directory.parent / "outputs" / name
            if item["mode"] == "clean":
                args += ["--output", str(output)]
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = cli.main(args)
            assert stdout.getvalue(), f"CLI returned {code}, stderr={stderr.getvalue()}"
            report = json.loads(stdout.getvalue())
            assert report["mode"] == item["mode"]
            assert _sha(src) == original == report["input"]["sha256"]
            results[name] = (code, report, src, output, stderr.getvalue())
        return results[name]
    return run


def _missing_skips(report, adapters):
    return [item for item in report["adapters_skipped"] if item["id"] in adapters]


def _assert_no_failed(report):
    assert not report["adapters_failed"], f"adapter failed: {report['adapters_failed']}"


def _match(finding, expectation):
    adapters = finding["adapter"]
    adapters = [adapters] if isinstance(adapters, str) else adapters
    return (finding["class"] == expectation["class"]
            and SEVERITY_RANK[finding["severity"]] >= SEVERITY_RANK[expectation["min_severity"]]
            and (not expectation.get("adapter_any")
                 or bool(set(adapters) & set(expectation["adapter_any"])))
            and ("channel" not in expectation
                 or finding["location"]["channel"] == expectation["channel"])
            and ("adapter_code" not in expectation
                 or finding["adapter_code"] == expectation["adapter_code"])
            and ("pointer" not in expectation
                 or finding["location"]["pointer"] == expectation["pointer"])
            and ("excerpt_contains" not in expectation
                 or expectation["excerpt_contains"] in (finding.get("excerpt") or ""))
            and ("decoded_contains" not in expectation
                 or expectation["decoded_contains"] in (finding["evidence"].get("decoded") or "")))


def _docx_text(path):
    with zipfile.ZipFile(path) as archive:
        return {name: [node.text or "" for node in ET.fromstring(archive.read(name)).iter(f"{{{W}}}t")]
                for name in archive.namelist() if name.startswith("word/") and name.endswith(".xml")}


def _trace_present(path, trace):
    kind, value = trace.split(":", 1) if ":" in trace else (trace, "")
    if kind.startswith("U+"):
        return chr(int(kind.removeprefix("U+"), 16)) in path.read_text(encoding="utf-8")
    if kind == "png-c2pa":
        return b"c2pa\x00ContentCredentials" in path.read_bytes()
    if kind in {"pdf-producer", "pdf-creator", "pdf-xmp"}:
        fitz = pytest.importorskip("pymupdf")
        with fitz.open(path) as pdf:
            text = (pdf.get_xml_metadata() if kind == "pdf-xmp"
                    else pdf.metadata[kind.removeprefix("pdf-")])
            return value in (text or "")
    with zipfile.ZipFile(path) as archive:
        if kind in {"creator", "lastModifiedBy", "Application", "description", "created", "modified"}:
            name = "docProps/app.xml" if kind == "Application" else "docProps/core.xml"
            root = ET.fromstring(archive.read(name))
            return any(node.tag.rsplit("}", 1)[-1] == kind and node.text == value for node in root.iter())
        if kind == "custom":
            if "docProps/custom.xml" not in archive.namelist():
                return False
            root = ET.fromstring(archive.read("docProps/custom.xml"))
            return any(node.get("name") == value for node in root.iter())
        if kind in {"div", "style"}:
            name = "word/webSettings.xml" if kind == "div" else "word/styles.xml"
            root = ET.fromstring(archive.read(name))
            attr = f"{{{W}}}" + ("id" if kind == "div" else "styleId")
            return any(node.tag == f"{{{W}}}{kind}" and node.get(attr) == value
                       for node in root.iter())
    raise AssertionError(f"unknown trace {trace}")


def test_manifest_is_complete_and_generator_is_reproducible(corpus_directory):
    names = [item["file"] for item in ENTRIES]
    assert len(names) == len(set(names))
    available = {item["file"] for item in ENTRIES
                 if (not item.get("optional_binary") or shutil.which(item["optional_binary"]))
                 and (not item.get("external_source") or (EXTERNAL / item["external_source"]).is_file())}
    assert available == {item.name for item in corpus_directory.iterdir() if item.is_file()}
    for item in ENTRIES:
        assert Path(item["file"]).name == item["file"]
        assert item["mode"] in {"detect", "clean"}
        assert item.get("must_find") or item.get("must_not_find") or item.get("expect_removed")
        if item["mode"] == "clean":
            assert item.get("visible_unchanged") is True
        for finding in item.get("must_find", []):
            assert finding["min_severity"] in SEVERITY_RANK
    second = corpus_directory.parent / "second-generation"
    proc = subprocess.run([sys.executable, "-B", str(GENERATOR), "--output", str(second)],
                          capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    assert {path.name: _sha(path) for path in second.iterdir()} == {
        path.name: _sha(path) for path in corpus_directory.iterdir()}


@pytest.mark.parametrize("item", _cases(DETECT))
def test_detect_corpus_findings(item, corpus_run):
    """Check available mechanisms even when an unrelated mandatory adapter is absent.

    The separate verdict test skips when *any* mandatory adapter is absent, so
    it never presents partial detection as a complete clean verdict.
    """
    _, report, _, _, _ = corpus_run(item)
    _assert_no_failed(report)
    if item.get("requires_binary") and not shutil.which(item["requires_binary"]):
        pytest.skip(f"required executable unavailable: {item['requires_binary']}")
    for expected in item.get("must_find", []):
        candidates = set(expected.get("adapter_any", []))
        if any(_match(f, expected) for f in report["findings"]):
            continue
        skipped = _missing_skips(report, candidates)
        if skipped:
            # An unavailable alternative could have supplied the finding; do
            # not mistake incomplete coverage for a demonstrated detector gap.
            pytest.skip("finding adapters unavailable: " + str(skipped))
        observed = [(f["class"], f["severity"], f["adapter"], f["location"]["channel"],
                     f["adapter_code"]) for f in report["findings"]]
        pytest.fail(f"missing {expected}; observed {observed}; skipped {report['adapters_skipped']}")
    assert all(f["severity"] not in item.get("must_not_find", []) for f in report["findings"]), report


@pytest.mark.parametrize("item", _cases(DETECT))
def test_detect_corpus_verdict(item, corpus_run):
    code, report, _, _, _ = corpus_run(item)
    _assert_no_failed(report)
    if item.get("requires_binary") and not shutil.which(item["requires_binary"]):
        pytest.skip(f"required executable unavailable: {item['requires_binary']}")
    mandatory = set(registry.DETECT_MATRIX[report["input"]["detected_type"]][0])
    skipped = _missing_skips(report, mandatory)
    if skipped:
        pytest.skip("mandatory detection adapters unavailable: " + str(skipped))
    if "expected_verdict" in item:
        assert report["verdict"] == item["expected_verdict"]
    substantive = [f for f in report["findings"] if f["class"] != "tool_error"]
    expected_code = int(any(SEVERITY_RANK[f["severity"]] >= SEVERITY_RANK["high"]
                            for f in substantive))
    assert code == expected_code


@pytest.mark.parametrize("item", _cases([item for item in ENTRIES if item["mode"] == "clean"]))
def test_clean_corpus(item, corpus_run):
    code, report, src, output, stderr = corpus_run(item)
    unavailable = [failure for failure in report["adapters_failed"]
                   if not registry.load(failure["id"]).available().available]
    if unavailable:
        pytest.skip("mandatory cleaner unavailable: " + str(unavailable))
    _assert_no_failed(report)
    signed = "signed-c2pa" in item["expect_removed"]
    if signed:
        assert report["c2pa"]["before"] is True, report
        if report["c2pa"]["after"] is False:
            assert code == 0 and report["verdict"] == item["expected_verdict"], (code, report, stderr)
            assert any(action.get("c2pa") and action["action"] == "removed"
                       for action in report["c2pa"]["actions"])
        else:
            assert report["c2pa"]["after"] is True
            assert code == 1 and report["verdict"] == "residual", (code, report, stderr)
            assert any(action.get("c2pa") and action["action"] == "residual"
                       for action in report["c2pa"]["actions"])
    else:
        assert code == 0 and report["verdict"] == item["expected_verdict"], (code, report, stderr)
    assert output.is_file() and report["output_path"] == str(output)
    for trace in item["expect_removed"]:
        if trace != "signed-c2pa":
            assert _trace_present(src, trace), f"fixture does not contain {trace}"
            assert not _trace_present(output, trace), f"clean output retains {trace}"
    if src.suffix in {".txt", ".md"}:
        assert output.read_text(encoding="utf-8") == item["expected_text"]
    elif src.suffix == ".docx":
        assert _docx_text(src) == _docx_text(output)
    elif src.suffix in {".xlsx", ".pptx"}:
        def visible(path):
            with zipfile.ZipFile(path) as archive:
                names = [name for name in archive.namelist() if name.endswith(".xml")
                         and name.startswith(("xl/worksheets/", "ppt/slides/"))]
                return {name: [node.text or "" for node in ET.fromstring(archive.read(name)).iter()
                               if node.tag.rsplit("}", 1)[-1] == "t"] for name in names}
        assert visible(src) == visible(output)
    elif src.suffix == ".pdf":
        fitz = pytest.importorskip("pymupdf")
        with fitz.open(src) as before, fitz.open(output) as after:
            assert len(before) == len(after)
            for page_before, page_after in zip(before, after):
                p = page_before.get_pixmap(colorspace=fitz.csRGB, alpha=False)
                q = page_after.get_pixmap(colorspace=fitz.csRGB, alpha=False)
                assert (p.width, p.height, p.samples) == (q.width, q.height, q.samples)
    elif src.suffix == ".png":
        fitz = pytest.importorskip("pymupdf")
        before, after = fitz.Pixmap(str(src)), fitz.Pixmap(str(output))
        assert (before.width, before.height, before.n, before.samples) == (
            after.width, after.height, after.n, after.samples)
        assert report["c2pa"]["before"] is True and report["c2pa"]["after"] is False
        assert any(action.get("c2pa") for action in report["c2pa"]["actions"])


@pytest.mark.parametrize("item", [item for item in ENTRIES if item.get("external_source")],
                         ids=lambda item: item["file"])
def test_signed_c2pa_preserves_decoded_pixels(item, corpus_run):
    image = pytest.importorskip("PIL.Image", reason="decoded C2PA pixel comparison needs Pillow")
    _, _, src, output, _ = corpus_run(item)

    def jpeg(path):
        if path.suffix.lower() == ".jpg":
            return path.read_bytes()
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist()
                     if name.startswith(("word/media/", "ppt/media/"))
                     and name.lower().endswith((".jpg", ".jpeg"))]
            assert len(names) == 1
            return archive.read(names[0])

    with image.open(io.BytesIO(jpeg(src))) as before, image.open(io.BytesIO(jpeg(output))) as after:
        before.load()
        after.load()
        assert (before.mode, before.size, before.tobytes()) == (after.mode, after.size, after.tobytes())


@pytest.mark.parametrize("item", [item for item in ENTRIES if item.get("known_gap")],
                         ids=lambda item: item["file"])
def test_known_gap_does_not_mask_tool_errors_or_input_mutation(item, corpus_run):
    """An xfail must not hide a crashed adapter, a missing output, or input mutation."""
    code, report, src, output, _ = corpus_run(item)
    _assert_no_failed(report)
    assert code in {0, 1}
    if item["mode"] == "clean":
        assert output.is_file() and report["output_path"] == str(output)
        if src.suffix == ".pdf":
            fitz = pytest.importorskip("pymupdf")
            with fitz.open(src) as before, fitz.open(output) as after:
                assert len(before) == len(after)
                for p, q in zip(before, after):
                    x, y = p.get_pixmap(alpha=False), q.get_pixmap(alpha=False)
                    assert (x.width, x.height, x.samples) == (y.width, y.height, y.samples)


def test_pdf_without_c2pa_does_not_report_c2pa_removal(corpus_run):
    item = next(entry for entry in ENTRIES if entry["file"] == "pdf-ai-producer-traces.pdf")
    _, report, _, _, _ = corpus_run(item)
    _assert_no_failed(report)
    assert report["c2pa"]["before"] is False and report["c2pa"]["after"] is False
    assert not report["c2pa"]["actions"]
