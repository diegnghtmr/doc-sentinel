"""S10: generated Word HTML-paste residue and preservation controls."""
from __future__ import annotations

import importlib
import struct
import zipfile
from xml.etree import ElementTree as ET

import pytest

from doc_sentinel.adapters.base import RunContext

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _adapter():
    return importlib.import_module("doc_sentinel.glue.ooxml_paste").OoxmlPasteResidueCleaner()


def _xml(inner, tag):
    return f'<w:{tag} xmlns:w="{W}">{inner}</w:{tag}>'.encode()


def _style(name, *, custom="1", title=None, extra=""):
    title = name if title is None else title
    return (f'<w:style w:type="character" w:customStyle="{custom}" w:styleId="{name}">'
            f'<w:name w:val="{title}"/>{extra}</w:style>')


def _fixture(path, *, referenced_div=False):
    styles = "".join(_style(name) for name in (
        "o00408", "s01997", "first-table-reference", "used", "base", "next", "link", "num"))
    styles += _style("Normal", custom="0", extra='<w:basedOn w:val="base"/>'
                     '<w:next w:val="next"/><w:link w:val="link"/>')
    styles += _style("MixedCase") + _style("different", title="another")
    refs = ('<w:pPr><w:pStyle w:val="used"/><w:numStyleId w:val="num"/>'
            + ('<w:divId w:val="42"/>' if referenced_div else '') + '</w:pPr>')
    body = f'<w:body><w:p>{refs}<w:r><w:t>ChatGPT — visible text</w:t></w:r></w:p></w:body>'
    web = '<w:divs>' + ''.join(f'<w:div w:id="{i}"/>' for i in range(290)) + '</w:divs>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", b"<Types/>", compress_type=zipfile.ZIP_STORED)
        z.writestr("word/document.xml", _xml(body, "document"), compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("word/webSettings.xml", _xml(web, "webSettings"),
                   compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("word/styles.xml", _xml(styles, "styles"), compress_type=zipfile.ZIP_STORED)
        z.writestr("word/comments.xml", _xml('<w:rStyle w:val="used"/>', "comments"))
        z.writestr("word/media/image.bin", b"unmodified\x00media")
    return path


def _contents(path):
    with zipfile.ZipFile(path) as z:
        return [(i.filename, i.compress_type, z.read(i)) for i in z.infolist()]


def _styles(data):
    root = ET.fromstring(data)
    return {el.get(f"{{{W}}}styleId") for el in root.findall(f"{{{W}}}style")}


def test_clean_removes_only_orphan_paste_residue_and_preserves_text(tmp_path):
    src, dest = _fixture(tmp_path / "in.docx"), tmp_path / "out.docx"
    before = _contents(src)
    adapter = _adapter()
    assert adapter.id == "ooxml-paste-residue" and adapter.mode == frozenset({"clean"})
    assert adapter.available().available
    findings = adapter.inspect(src, RunContext(workdir=tmp_path))
    assert findings and all(f.cls == "document_metadata" for f in findings)
    assert all(f.location.channel == "metadata" for f in findings)
    result = adapter.clean(src, dest, RunContext(workdir=tmp_path))
    after = _contents(dest)
    assert result.changed and not result.residual
    assert [item[:2] for item in after] == [item[:2] for item in before]
    for old, new in zip(before, after):
        if old[0] not in ("word/webSettings.xml", "word/styles.xml"):
            assert old == new
    current = {name: data for name, _, data in after}
    assert not ET.fromstring(current["word/webSettings.xml"]).findall(f"{{{W}}}divs")
    remaining = _styles(current["word/styles.xml"])
    assert not ({"o00408", "s01997", "first-table-reference"} & remaining)
    assert {"used", "base", "next", "link", "num", "Normal", "MixedCase", "different"} <= remaining
    assert b"ChatGPT \xe2\x80\x94 visible text" in current["word/document.xml"]
    assert any(a["count"] == 290 and "div" in a["what"].lower() for a in result.actions)
    assert any(a["count"] == 3 and set(a.get("names", [])) ==
               {"o00408", "s01997", "first-table-reference"} for a in result.actions)
    assert _contents(src) == before


def test_referenced_divs_and_styles_survive(tmp_path):
    src, dest = _fixture(tmp_path / "in.docx", referenced_div=True), tmp_path / "out.docx"
    original = {name: data for name, _, data in _contents(src)}
    result = _adapter().clean(src, dest, RunContext(workdir=tmp_path))
    updated = {name: data for name, _, data in _contents(dest)}
    assert result.changed
    assert updated["word/webSettings.xml"] == original["word/webSettings.xml"]
    assert {"used", "base", "next", "link", "num"} <= _styles(updated["word/styles.xml"])


def test_clean_control_and_unsupported_format_are_exact_copies(tmp_path):
    src = tmp_path / "clean.docx"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("word/document.xml", _xml('<w:body><w:p><w:r><w:t>Safe</w:t>'
                                             '</w:r></w:p></w:body>', "document"))
        z.writestr("word/styles.xml", _xml(_style("Normal", custom="0"), "styles"))
    adapter = _adapter()
    dest = tmp_path / "out.docx"
    result = adapter.clean(src, dest, RunContext(workdir=tmp_path))
    assert not result.changed and result.actions == [] and dest.read_bytes() == src.read_bytes()
    other, copied = tmp_path / "plain.txt", tmp_path / "copy.txt"
    other.write_text("literal", encoding="utf-8")
    result = adapter.clean(other, copied, RunContext(workdir=tmp_path))
    assert not result.changed and result.actions == [] and copied.read_bytes() == other.read_bytes()
    with pytest.raises(ValueError):
        adapter.clean(src, src, RunContext(workdir=tmp_path))


def test_non_word_zip_is_copied_even_with_docx_suffix(tmp_path):
    src, dest = tmp_path / "not-word.docx", tmp_path / "out.docx"
    with zipfile.ZipFile(src, "w") as archive:
        archive.writestr("xl/workbook.xml", b"<workbook/>")
        archive.writestr("word/styles.xml", _xml(_style("unused"), "styles"))
    adapter = _adapter()
    assert adapter.inspect(src, RunContext(workdir=tmp_path)) == []
    result = adapter.clean(src, dest, RunContext(workdir=tmp_path))
    assert not result.changed and dest.read_bytes() == src.read_bytes()


def test_zip_bomb_declared_uncompressed_size_is_rejected(tmp_path):
    src, dest = tmp_path / "bomb.docx", tmp_path / "out.docx"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("word/document.xml", _xml("", "document"))
    data = bytearray(src.read_bytes())
    central = data.index(b"PK\x01\x02")
    struct.pack_into("<I", data, central + 24, 129 * 1024 * 1024)
    src.write_bytes(data)
    with pytest.raises((ValueError, RuntimeError)):
        _adapter().clean(src, dest, RunContext(workdir=tmp_path))
    assert not dest.exists()
