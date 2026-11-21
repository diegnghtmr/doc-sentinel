"""A real PDF clean must not change the visible first page (PLAN §4, §15)."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json

import pytest

from doc_sentinel import cli, registry


@pytest.mark.integration
def test_tounicode_substitution_changes_extraction_not_render(corpus_directory):
    """Prove A3 ground truth independently of any scanner's finding."""
    fitz = pytest.importorskip("pymupdf")
    with (fitz.open(corpus_directory / "pdf-tounicode-control.pdf") as honest,
          fitz.open(corpus_directory / "pdf-tounicode-a3.pdf") as tampered):
        assert honest[0].get_text().strip() == "amount maryland"
        assert tampered[0].get_text().strip() == "adount daryland"
        p = honest[0].get_pixmap(colorspace=fitz.csRGB, alpha=False)
        q = tampered[0].get_pixmap(colorspace=fitz.csRGB, alpha=False)
        assert (p.width, p.height, p.samples) == (q.width, q.height, q.samples)


@pytest.mark.integration
def test_pdf_clean_preserves_rendered_first_page(corpus_directory):
    fitz = pytest.importorskip("pymupdf", reason="PDF round-trip needs PyMuPDF")
    src = corpus_directory / "pdf-plain.pdf"
    dest = corpus_directory.parent / "roundtrip.pdf"
    original = hashlib.sha256(src.read_bytes()).hexdigest()
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli.main(["clean", str(src), "--output", str(dest), "--json"])
    assert stdout.getvalue(), stderr.getvalue()
    report = json.loads(stdout.getvalue())
    unavailable = [entry for entry in report["adapters_failed"]
                   if not registry.load(entry["id"]).available().available]
    if unavailable:
        pytest.skip("mandatory cleaner unavailable: " + str(unavailable))
    assert not report["adapters_failed"]
    assert code == 0 and report["verdict"] in {"cleaned", "already_clean"}
    assert dest.is_file() and report["output_path"] == str(dest)
    assert hashlib.sha256(src.read_bytes()).hexdigest() == original == report["input"]["sha256"]
    with fitz.open(src) as before, fitz.open(dest) as after:
        p = before[0].get_pixmap(colorspace=fitz.csRGB, alpha=False)
        q = after[0].get_pixmap(colorspace=fitz.csRGB, alpha=False)
    assert (p.width, p.height, p.n) == (q.width, q.height, q.n)
    assert min(p.samples) < 200, "the original page must contain visible ink"
    differences = [abs(a - b) for a, b in zip(p.samples, q.samples)]
    assert sum(differences) / len(differences) < 0.1
    assert sum(bool(value) for value in differences) / len(differences) < 0.005
