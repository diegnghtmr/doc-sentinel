"""S10: python-docx default fingerprints remaining after the upstream clean."""
from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import zipfile

import pytest

from doc_sentinel.adapters.base import RunContext

TEMPLATE_DATE = b"2013-12-23T23:15:00Z"


def _fingerprint():
    return importlib.import_module("doc_sentinel.glue.ooxml_fingerprint").OoxmlFingerprintCleaner()


def _generated(path):
    docx = pytest.importorskip("docx")
    doc = docx.Document()
    doc.add_paragraph("Visible content: ChatGPT — not metadata.")
    doc.save(path)
    return path


def _read(path):
    with zipfile.ZipFile(path) as archive:
        return [(item.filename, item.compress_type, archive.read(item))
                for item in archive.infolist()]


def _rewrite(source, dest, replacements):
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(dest, "w") as new:
        for item in old.infolist():
            new.writestr(item, replacements.get(item.filename, old.read(item)))


@pytest.mark.integration
def test_upstream_leaves_default_dates_and_thumbnail_on_generated_docx(tmp_path):
    mod = importlib.import_module("doc_sentinel.adapters.watermarks_remover")
    upstream = mod.WatermarksRemoverAdapter()
    if not upstream.available().available:
        pytest.skip(upstream.available().reason)
    src, dest = _generated(tmp_path / "input.docx"), tmp_path / "wmr.docx"
    upstream.clean(src, dest, RunContext(workdir=tmp_path))
    parts = {name: data for name, _, data in _read(dest)}
    assert parts["docProps/core.xml"].count(TEMPLATE_DATE) == 2
    assert parts["docProps/thumbnail.jpeg"]
    print("generated python-docx default thumbnail sha256:",
          hashlib.sha256(parts["docProps/thumbnail.jpeg"]).hexdigest())


def test_remove_default_dates_and_thumbnail_preserving_package(tmp_path):
    src, dest = _generated(tmp_path / "input.docx"), tmp_path / "output.docx"
    original = _read(src)
    adapter = _fingerprint()
    assert adapter.id == "ooxml-fingerprint" and adapter.mode == frozenset({"clean"})
    assert adapter.available().available
    findings = adapter.inspect(src, RunContext(workdir=tmp_path))
    assert findings and all(f.cls == "document_metadata" and f.location.channel == "metadata"
                            for f in findings)
    result = adapter.clean(src, dest, RunContext(workdir=tmp_path))
    changed = _read(dest)
    before = {name: (ctype, data) for name, ctype, data in original}
    after = {name: (ctype, data) for name, ctype, data in changed}
    assert result.changed and not result.residual
    assert TEMPLATE_DATE not in after["docProps/core.xml"][1]
    assert "docProps/thumbnail.jpeg" not in after
    assert b"thumbnail.jpeg" not in after["_rels/.rels"][1]
    assert [name for name, _, _ in changed] == [name for name, _, _ in original
                                                if name != "docProps/thumbnail.jpeg"]
    for name in before.keys() & after.keys():
        assert before[name][0] == after[name][0]
        if name not in ("docProps/core.xml", "_rels/.rels", "[Content_Types].xml"):
            assert before[name][1] == after[name][1]
    assert any(a["action"] == "removed" and a["count"] == 2 for a in result.actions)
    assert any("thumbnail" in a["what"].lower() for a in result.actions)
    docx = pytest.importorskip("docx")
    assert docx.Document(dest).paragraphs[0].text == "Visible content: ChatGPT — not metadata."
    assert _read(src) == original


def test_non_default_dates_and_thumbnail_are_preserved_exactly(tmp_path):
    src = _generated(tmp_path / "default.docx")
    parts = {name: data for name, _, data in _read(src)}
    core = parts["docProps/core.xml"].replace(TEMPLATE_DATE, b"2020-03-14T10:20:30Z")
    customized, dest = tmp_path / "custom.docx", tmp_path / "out.docx"
    _rewrite(src, customized, {"docProps/core.xml": core,
                               "docProps/thumbnail.jpeg": b"user thumbnail"})
    result = _fingerprint().clean(customized, dest, RunContext(workdir=tmp_path))
    assert not result.changed and result.actions == []
    assert dest.read_bytes() == customized.read_bytes()
    with pytest.raises(ValueError):
        _fingerprint().clean(customized, customized, RunContext(workdir=tmp_path))


def test_fingerprint_is_registered_only_after_upstream_for_docx():
    from doc_sentinel import registry

    assert registry.CLEAN_MATRIX["docx"][:2] == ("watermarks-remover", "ooxml-fingerprint")
    assert "ooxml-paste-residue" in registry.CLEAN_MATRIX["docx"]
    assert registry.load("ooxml-fingerprint").available().available


def test_non_word_zip_is_copied_even_with_docx_suffix(tmp_path):
    src = tmp_path / "not-a-word-file.docx"
    dest = tmp_path / "copy.docx"
    with zipfile.ZipFile(src, "w") as archive:
        archive.writestr("xl/workbook.xml", b"<workbook/>")
        archive.writestr("docProps/core.xml", b'<cp:coreProperties '
                         b'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                         b'xmlns:dcterms="http://purl.org/dc/terms/">'
                         b'<dcterms:created>2013-12-23T23:15:00Z</dcterms:created>'
                         b'<dcterms:modified>2013-12-23T23:15:00Z</dcterms:modified>'
                         b'</cp:coreProperties>')
    adapter = _fingerprint()
    assert adapter.inspect(src, RunContext(workdir=tmp_path)) == []
    result = adapter.clean(src, dest, RunContext(workdir=tmp_path))
    assert not result.changed and dest.read_bytes() == src.read_bytes()


def test_default_thumbnail_used_as_content_is_not_removed(tmp_path):
    original = _generated(tmp_path / "original.docx")
    parts = {name: data for name, _, data in _read(original)}
    rels = parts["_rels/.rels"].replace(b"relationships/metadata/thumbnail",
                                        b"relationships/image")
    assert rels != parts["_rels/.rels"]
    src, dest = tmp_path / "input.docx", tmp_path / "output.docx"
    _rewrite(original, src, {"_rels/.rels": rels})
    result = _fingerprint().clean(src, dest, RunContext(workdir=tmp_path))
    changed = {name: data for name, _, data in _read(dest)}
    assert result.changed and "docProps/thumbnail.jpeg" in changed
    assert changed["docProps/thumbnail.jpeg"] == parts["docProps/thumbnail.jpeg"]
    assert changed["_rels/.rels"] == rels


@pytest.mark.integration
def test_generated_document_runs_actual_clean_chain_and_keeps_visible_text(tmp_path, capsys):
    from doc_sentinel import cli
    from doc_sentinel.adapters.watermarks_remover import WatermarksRemoverAdapter

    adapter = WatermarksRemoverAdapter()
    if not adapter.available().available:
        pytest.skip(adapter.available().reason)
    template = _generated(tmp_path / "template.docx")
    parts = {name: data for name, _, data in _read(template)}
    styles = parts["word/styles.xml"].replace(
        b"</w:styles>", b'<w:style w:type="character" w:customStyle="1" w:styleId="o00408">'
        b'<w:name w:val="o00408"/></w:style></w:styles>')
    web = parts["word/webSettings.xml"].replace(
        b"</w:webSettings>", b'<w:divs><w:div w:id="42"/></w:divs></w:webSettings>')
    assert styles != parts["word/styles.xml"] and web != parts["word/webSettings.xml"]
    src, dest = tmp_path / "input.docx", tmp_path / "clean.docx"
    _rewrite(template, src, {"word/styles.xml": styles, "word/webSettings.xml": web})
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    code = cli.main(["clean", str(src), "--json", "-o", str(dest)])
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert code == 0 and report["verdict"] == "cleaned"
    assert captured.err == ("doc-sentinel clean: verdict=cleaned\nfindings: none\n"
                            f"output: {dest}\n")
    assert report["adapters_run"] == ["watermarks-remover", "ooxml-fingerprint",
                                      "ooxml-paste-residue"]
    assert report["c2pa"]["before"] is False and report["c2pa"]["after"] is False
    assert not report["adapters_failed"] and adapter.inspect(dest, RunContext(workdir=tmp_path)) == []
    assert hashlib.sha256(src.read_bytes()).hexdigest() == digest
    docx = pytest.importorskip("docx")
    assert docx.Document(dest).paragraphs[0].text == "Visible content: ChatGPT — not metadata."


@pytest.mark.integration
def test_generated_docx_libreoffice_visible_text_round_trip(tmp_path):
    from doc_sentinel.adapters.base import run_tool
    from doc_sentinel.pipeline import run_clean

    executable = shutil.which("soffice")
    if executable is None:
        pytest.skip("LibreOffice is unavailable")
    src, dest = _generated(tmp_path / "original.docx"), tmp_path / "cleaned.docx"
    report, code = run_clean(src, output=dest)
    if report["adapters_failed"] and "missing watermarks-remover" in str(report["adapters_failed"]):
        pytest.skip("watermarks-remover unavailable")
    assert code == 0
    ctx = RunContext(workdir=tmp_path)
    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    after.mkdir()
    profile = "-env:UserInstallation=" + (tmp_path / "lo-profile").as_uri()
    for document, output in ((src, before), (dest, after)):
        run_tool([executable, profile, "--headless", "--convert-to", "txt", "--outdir",
                  str(output), str(document)], ctx, ok_codes=(0,))
    original_text = (before / "original.txt").read_text(encoding="utf-8-sig")
    cleaned_text = (after / "cleaned.txt").read_text(encoding="utf-8-sig")
    assert original_text == cleaned_text
    assert "Visible content: ChatGPT — not metadata." in cleaned_text
