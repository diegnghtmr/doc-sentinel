"""Read-only extraction of document channels for Unicode and Prompt Guard (PLAN §2.2)."""
from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

from doc_sentinel.adapters.base import AdapterError, RunContext, run_tool
from doc_sentinel.schema import Channel

_MAX_TEXT = 2_000_000
_MAX_ARCHIVE = 200 * 1024 * 1024
_MAX_PART = 10 * 1024 * 1024
_MAX_ENTRIES = 10_000
_MAX_OCR_IMAGES = 20
_MAX_PAGES = 500
_WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"


def _append(channels: list[Channel], kind: str, text: object, *, pointer: str | None = None,
            page: int | None = None, hidden: bool = False) -> None:
    if text is not None:
        value = str(text)[:_MAX_TEXT]
        if value.strip():
            channels.append(Channel(kind, value, pointer=pointer, page=page, hidden=hidden))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse(data: bytes) -> ET.Element:
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("XML declarations with entities/DTD are not supported")
    return ET.fromstring(data)


def _props(channels: list[Channel], root: ET.Element) -> None:
    for child in root:
        name = child.get("name") if _local(child.tag) == "property" else _local(child.tag)
        _append(channels, "metadata", "".join(child.itertext()), pointer=name)


class _HTMLChannels(HTMLParser):
    def __init__(self, channels: list[Channel]) -> None:
        super().__init__(convert_charrefs=True)
        self.channels = channels
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name in {"alt", "title"}:
                _append(self.channels, "alt", value, pointer=f"{tag}@{name}")
            elif name == "href":
                _append(self.channels, "hyperlink", value, pointer=f"{tag}@href")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_comment(self, data: str) -> None:
        _append(self.channels, "html_hidden", data, pointer="comment", hidden=True)

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def _ocr(channels: list[Channel], *, data: bytes | None = None, src: Path | None = None,
         pointer: str, page: int | None = None) -> None:
    executable = shutil.which("tesseract")
    if not executable or (data is not None and len(data) > _MAX_PART):
        return
    with tempfile.TemporaryDirectory(prefix="doc-sentinel-ocr-") as tmp:
        workdir = Path(tmp)
        if data is not None:
            image = workdir / "image"
            image.write_bytes(data)
        elif src is not None:
            image = src.resolve()
        else:
            return
        ctx = RunContext(workdir=workdir, timeout_s=20)
        try:
            proc = run_tool([executable, str(image), "stdout", "-l", "spa+eng"], ctx,
                            ok_codes=(0,))
        except AdapterError:
            # OCR is optional: missing language data, invalid images, or timeout mean skipped.
            return
        _append(channels, "image_ocr", proc.stdout, pointer=pointer, page=page)


def _word_part(channels: list[Channel], name: str, root: ET.Element) -> None:
    if name == "word/comments.xml":
        for comment in root.iter(f"{{{_WORD}}}comment"):
            text = "".join(node.text or "" for node in comment.iter(f"{{{_WORD}}}t"))
            _append(channels, "comment", text, pointer=f"{name}#{comment.get(f'{{{_WORD}}}id', '')}",
                    hidden=True)
        return
    if name.endswith(".rels"):
        for rel in root:
            if (rel.get("TargetMode", "").lower() == "external"
                    and rel.get("Type", "").endswith("/hyperlink")):
                _append(channels, "hyperlink", rel.get("Target"),
                        pointer=f"{name}#{rel.get('Id', '')}")
        return
    for node in root.iter(f"{{{_DRAWING}}}docPr"):
        for attr in ("descr", "title"):
            _append(channels, "alt", node.get(attr), pointer=f"{name}#docPr@{attr}")
    for deletion in root.iter(f"{{{_WORD}}}del"):
        text = "".join(node.text or "" for node in deletion.iter()
                       if node.tag in {f"{{{_WORD}}}t", f"{{{_WORD}}}delText"})
        _append(channels, "tracked_change", text, pointer=name, hidden=True)
    text = "".join(node.text or "" for node in root.iter(f"{{{_WORD}}}t"))
    _append(channels, "body", text, pointer=name)


def _archive(src: Path, dtype: str, channels: list[Channel]) -> None:
    with zipfile.ZipFile(src) as archive:
        entries = archive.infolist()
        if len(entries) > _MAX_ENTRIES or sum(i.file_size for i in entries) > _MAX_ARCHIVE:
            raise ValueError("archive exceeds uncompressed size or entry limit")
        images = 0
        for info in entries:
            name = info.filename
            if info.is_dir():
                continue
            is_props = name in {"docProps/core.xml", "docProps/app.xml", "docProps/custom.xml"}
            word_part = dtype == "docx" and bool(re.fullmatch(
                r"word/(?:document|comments|header\d+|footer\d+|footnotes|endnotes)\.xml", name))
            rels = dtype == "docx" and name.startswith("word/") and name.endswith(".rels")
            other_xml = dtype != "docx" and name.lower().endswith((".xml", ".xhtml", ".html"))
            image = dtype == "docx" and name.startswith("word/media/")
            if dtype == "docx" and name.startswith("word/embeddings/"):
                _append(channels, "embedded", Path(name).name, pointer=name)
            if image and images < _MAX_OCR_IMAGES:
                images += 1
                if info.file_size <= _MAX_PART:
                    _ocr(channels, data=archive.read(info), pointer=name)
            if not (is_props or word_part or rels or other_xml):
                continue
            if info.file_size > _MAX_PART:
                raise ValueError(f"archive part exceeds uncompressed size limit: {name}")
            data = archive.read(info)
            try:
                root = _parse(data)
            except ET.ParseError:
                # Best effort for unrelated unsupported or malformed XML parts.
                continue
            if is_props:
                _props(channels, root)
            elif word_part or rels:
                _word_part(channels, name, root)
            else:
                _append(channels, "body", " ".join(root.itertext()), pointer=name)


def _pdf(src: Path, channels: list[Channel]) -> None:
    from doc_sentinel._pymupdf import load as _load_pymupdf
    pymupdf = _load_pymupdf()

    with pymupdf.open(src) as pdf:
        if len(pdf) > _MAX_PAGES:
            raise ValueError("PDF exceeds page limit")
        for key, value in (pdf.metadata or {}).items():
            _append(channels, "metadata", value, pointer=key)
        _append(channels, "metadata", pdf.get_xml_metadata(), pointer="XMP")
        for name in pdf.embfile_names():
            info = pdf.embfile_info(name)
            _append(channels, "embedded", name, pointer="embedded filename")
            _append(channels, "embedded", info.get("description"), pointer=f"{name}#description")
        image_count = 0
        for page_index, page in enumerate(pdf):
            page_num = page_index + 1
            _append(channels, "body", page.get_text(), pointer="page text", page=page_num)
            for annot in page.annots() or ():
                _append(channels, "annotation", annot.info.get("content"),
                        pointer=f"annotation:{annot.xref}", page=page_num, hidden=True)
                if annot.type[0] == pymupdf.PDF_ANNOT_FILE_ATTACHMENT:
                    info = annot.file_info or {}
                    for key in ("filename", "description"):
                        _append(channels, "embedded", info.get(key),
                                pointer=f"annotation:{annot.xref}#{key}", page=page_num)
            for link in page.get_links():
                _append(channels, "hyperlink", link.get("uri"), pointer="link URI", page=page_num)
            for widget in page.widgets() or ():
                _append(channels, "form_field", widget.field_value, pointer=widget.field_name,
                        page=page_num)
            for image in page.get_images():
                if image_count >= _MAX_OCR_IMAGES:
                    break
                image_count += 1
                item = pdf.extract_image(image[0])
                if item:
                    _ocr(channels, data=item["image"], pointer=f"image:{image[0]}", page=page_num)


def extract_channels(src: Path, dtype: str) -> list[Channel]:
    channels = [Channel("filename", src.name, pointer="filename")]
    if dtype in {"txt", "md", "html", "svg", "eml"}:
        with src.open("rb") as stream:
            text = stream.read(_MAX_TEXT).decode("utf-8", errors="replace")
        if dtype == "html":
            parser = _HTMLChannels(channels)
            parser.feed(text)
            _append(channels, "body", "".join(parser.text), pointer="body")
        else:
            # Preserve the plain-text interface, including an empty body.
            channels.append(Channel("body", text, pointer="body"))
    elif dtype in {"docx", "xlsx", "pptx", "odt", "epub"}:
        _archive(src, dtype, channels)
    elif dtype == "pdf":
        _pdf(src, channels)
    elif dtype == "image":
        _ocr(channels, src=src, pointer="image")
    return channels
