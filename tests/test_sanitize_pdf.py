"""PDFs are not edited until a position-preserving stream editor is available."""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import pytest

from doc_sentinel.sanitize import sanitize


@pytest.fixture
def pdf_paths():
    pymupdf = pytest.importorskip("pymupdf")
    with tempfile.TemporaryDirectory(prefix="doc-sentinel-sanitize-pdf-") as directory:
        root = Path(directory)
        src, dest = root / "source.pdf", root / "output.pdf"
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((30, 50), "Visible content")
            src.write_bytes(document.tobytes())
        yield root, src, dest


def test_pdf_is_unsupported_and_leaves_original_unchanged(pdf_paths):
    workdir, src, dest = pdf_paths
    before = hashlib.sha256(src.read_bytes()).digest()
    result = sanitize(src, dest, workdir=workdir)
    assert result.status == "unsupported" and result.refusal_reason
    assert not dest.exists() and hashlib.sha256(src.read_bytes()).digest() == before


def test_pdf_glyph_divergence_is_refused_without_guessing(pdf_paths):
    workdir, src, dest = pdf_paths
    before = hashlib.sha256(src.read_bytes()).digest()
    result = sanitize(src, dest, workdir=workdir, has_glyph_divergence=True)
    assert result.status == "refused" and "glyph" in result.refusal_reason.lower()
    assert not dest.exists() and hashlib.sha256(src.read_bytes()).digest() == before
