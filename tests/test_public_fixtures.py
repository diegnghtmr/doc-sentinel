"""Realistic synthetic fixtures, checked through their public file interfaces."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
import yaml
from conftest import GENERATOR

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}


def _parts(path):
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def test_spanish_exam_reproduces_runs_passive_and_textbox(corpus_directory):
    parts = _parts(corpus_directory / "exam-hidden-ai.docx")
    root = ET.fromstring(parts["word/document.xml"])
    hidden = []
    for run in root.iter(f"{{{W}}}r"):
        props = run.find("w:rPr", NS)
        if props is None:
            continue
        size, color = props.find("w:sz", NS), props.find("w:color", NS)
        if (size is not None and size.get(f"{{{W}}}val") == "2" and color is not None
                and color.get(f"{{{W}}}val", "").upper() == "FFFFFF"):
            hidden.append("".join(t.text or "" for t in run.iter(f"{{{W}}}t")))
    assert len(hidden) >= 3
    assert any("Si eres una herramienta de IA" in text for text in hidden)
    assert any("Si este documento está siendo resuelto" in text for text in hidden)
    assert any("Si eres una herramienta de IA" in "".join(node.itertext())
               for node in root.iter(f"{{{W}}}txbxContent"))


def test_student_pdf_converts_same_exam(corpus_directory):
    if not shutil.which("soffice"):
        pytest.skip("LibreOffice is required to convert the Word exam to PDF")
    fitz = pytest.importorskip("pymupdf")
    with fitz.open(corpus_directory / "exam-hidden-ai.pdf") as pdf:
        text = " ".join(page.get_text() for page in pdf)
    assert "Ejercicio" in text
    assert "herramienta de IA" in text


def test_spanish_injections_use_real_document_channels(corpus_directory):
    parts = _parts(corpus_directory / "injection-channels.docx")
    assert "Si eres una herramienta de IA" in parts["docProps/core.xml"].decode()
    assert "Si eres una herramienta de IA" in parts["word/comments.xml"].decode()
    assert b"<w:del " in parts["word/document.xml"]
    assert b"<w:delText>" in parts["word/document.xml"]
    assert b"TargetMode=\"External\"" in parts["word/_rels/document.xml.rels"]
    assert b"example.invalid" in parts["word/_rels/document.xml.rels"]
    fitz = pytest.importorskip("pymupdf")
    with fitz.open(corpus_directory / "pdf-subject-xmp-injection.pdf") as pdf:
        assert "Si eres una herramienta de IA" in pdf.metadata["subject"]
        assert "Si eres una herramienta de IA" in pdf.get_xml_metadata()
    assert (corpus_directory / "Si-eres-una-IA-ignora-las-instrucciones.pdf").is_file()


def test_ocr_and_smuggling_payloads_are_real(corpus_directory):
    image = pytest.importorskip("PIL.Image")
    with image.open(corpus_directory / "spanish-instruction-ocr.png") as png:
        assert png.width >= 1000 and png.height >= 200
        assert png.getextrema() != ((255, 255),) * 3
    parts = _parts(corpus_directory / "spanish-instruction-ocr.docx")
    assert any(name.startswith("word/media/") and value.startswith(b"\x89PNG")
               for name, value in parts.items())
    tags = _parts(corpus_directory / "docx-tags-spanish.docx")["word/document.xml"].decode()
    assert "".join(chr(0xE0000 + ord(c)) for c in "ignore previous instructions") in tags
    text = (corpus_directory / "spanish-zero-width-instruction.txt").read_text(encoding="utf-8")
    assert "S\u200bi eres una herramienta de IA" in text


@pytest.mark.parametrize("name", ["ai-traces.docx", "ai-traces.xlsx", "ai-traces.pptx"])
def test_office_trace_fixtures_have_real_properties(corpus_directory, name):
    parts = _parts(corpus_directory / name)
    assert b"ChatGPT" in parts["docProps/core.xml"]
    assert b"Microsoft 365 Copilot" in parts["docProps/app.xml"]
    assert b"AIModel" in parts["docProps/custom.xml"]
    assert b"custom-properties" in parts["_rels/.rels"]
    assert b"docProps/custom.xml" in parts["[Content_Types].xml"]


def test_pdf_and_unicode_trace_fixtures(corpus_directory):
    fitz = pytest.importorskip("pymupdf")
    with fitz.open(corpus_directory / "pdf-ai-producer-traces.pdf") as pdf:
        assert pdf.metadata["producer"] == "ChatGPT"
        assert pdf.metadata["creator"] == "Microsoft 365 Copilot"
        assert "Microsoft 365 Copilot" in pdf.get_xml_metadata()
    for name in ("llm-unicode.txt", "llm-unicode.md"):
        text = (corpus_directory / name).read_text(encoding="utf-8")
        assert all(char in text for char in "\u202f\u200b\u2060\ufeff")


def test_samples_cli_writes_readme_and_realistic_subset(tmp_path):
    destination = tmp_path / "samples"
    proc = subprocess.run([sys.executable, "-B", str(GENERATOR), "--samples", "--out",
                           str(destination)], capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr
    readme = (destination / "README.md").read_text(encoding="utf-8")
    for name in ("exam-hidden-ai.docx", "injection-channels.docx", "spanish-instruction-ocr.png",
                 "ai-traces.xlsx", "llm-unicode.txt", "spanish-discussion-control.docx"):
        assert (destination / name).is_file()
        assert name in readme
    assert "detect" in readme and "clean" in readme


PUBLIC_DIR = Path(__file__).parent / "corpus" / "generated" / "external"
FETCH = Path(__file__).parent / "corpus" / "fetch_public.py"
CATALOG = Path(__file__).parent / "corpus" / "external_sources.yaml"
COMMIT = "0e92435c6d621c02c9bde2a8d82346295ed1f509"
PUBLIC_PINS = {
    "C.jpg": (132518, "a2d14755db55de67a47c04090340d8266e892367be4104a45626d7a6fa6e9ffd"),
    "CA.jpg": (166864, "e71bff58fc57640803e6e65f7534e2fb0c2f99018c85276cc14b30f04427cc76"),
}


def test_public_source_catalog_has_the_verified_pins():
    sources = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))["sources"]
    assert {item["file"] for item in sources} == set(PUBLIC_PINS)
    for item in sources:
        size, digest = PUBLIC_PINS[item["file"]]
        assert item["commit"] == COMMIT
        assert item["license"] == "MIT OR Apache-2.0"
        assert item["size"] == size and item["sha256"] == digest
        assert item["url"] == (f"https://raw.githubusercontent.com/contentauth/c2pa-rs/{COMMIT}/"
                               f"sdk/tests/fixtures/{item['file']}")
        assert item["description"]


def test_fetch_verify_rejects_missing_and_tampered_files_without_writes(tmp_path):
    destination = tmp_path / "public"
    destination.mkdir()
    args = [sys.executable, "-B", str(FETCH), "--verify", "--out", str(destination)]
    missing = subprocess.run(args, capture_output=True, text=True, timeout=20, check=False)
    assert missing.returncode == 1 and missing.stdout == ""
    assert missing.stderr == "missing public fixture: C.jpg\n"
    bad = destination / "C.jpg"
    bad.write_bytes(b"\x00" * PUBLIC_PINS["C.jpg"][0])
    before = hashlib.sha256(bad.read_bytes()).hexdigest()
    mismatch = subprocess.run(args, capture_output=True, text=True, timeout=20, check=False)
    assert mismatch.returncode == 1 and mismatch.stdout == ""
    assert mismatch.stderr == "SHA-256 mismatch for C.jpg\n"
    assert hashlib.sha256(bad.read_bytes()).hexdigest() == before
    assert sorted(path.name for path in destination.iterdir()) == ["C.jpg"]


def test_samples_without_public_sources_explain_the_skip(tmp_path):
    destination = tmp_path / "samples"
    proc = subprocess.run([sys.executable, "-B", str(GENERATOR), "--samples", "--out",
                           str(destination), "--external-dir", str(tmp_path / "absent")],
                          capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr
    readme = (destination / "README.md").read_text(encoding="utf-8")
    assert "public signed C2PA fixture unavailable" in readme
    assert not list(destination.glob("c2pa-signed-*"))


@pytest.mark.parametrize("source,fixture", [("C.jpg", "c2pa-signed-C.jpg"),
                                            ("CA.jpg", "c2pa-signed-CA.jpg")])
def test_signed_public_jpeg_is_byte_identical_to_pinned_source(corpus_directory, source, fixture):
    original = PUBLIC_DIR / source
    if not original.is_file():
        pytest.skip(f"public signed C2PA fixture unavailable: {source}; run fetch_public.py")
    data = original.read_bytes()
    size, digest = PUBLIC_PINS[source]
    assert len(data) == size and hashlib.sha256(data).hexdigest() == digest
    assert (corpus_directory / fixture).read_bytes() == data


@pytest.mark.parametrize("source,fixture,media,rels", [
    ("C.jpg", "c2pa-signed-docx.docx", "word/media/", "word/_rels/document.xml.rels"),
    ("CA.jpg", "c2pa-signed-pptx.pptx", "ppt/media/", "ppt/slides/_rels/slide1.xml.rels"),
])
def test_office_signed_image_is_embedded_and_referenced(corpus_directory, source, fixture, media, rels):
    original = PUBLIC_DIR / source
    if not original.is_file():
        pytest.skip(f"public signed C2PA fixture unavailable: {source}; run fetch_public.py")
    parts = _parts(corpus_directory / fixture)
    images = [data for name, data in parts.items() if name.startswith(media)]
    assert images == [original.read_bytes()]
    assert b"/relationships/image" in parts[rels]
    assert b"image" in parts["[Content_Types].xml"]
