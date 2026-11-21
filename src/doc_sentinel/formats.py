"""Input type detection by content (magic bytes), with the suffix only as a tie-breaker."""

from __future__ import annotations

import zipfile
from pathlib import Path

_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image"), (b"\xff\xd8\xff", "image"), (b"GIF8", "image"),
    (b"II*\x00", "image"), (b"MM\x00*", "image"), (b"BM", "image"),
)


def _zip_type(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            if "mimetype" in names:
                mime = zf.read("mimetype").decode("ascii", "replace").strip()
                if mime == "application/epub+zip":
                    return "epub"
                if mime.startswith("application/vnd.oasis.opendocument"):
                    return "odt"
            if any(n.startswith("word/") for n in names):
                return "docx"
            if any(n.startswith("xl/") for n in names):
                return "xlsx"
            if any(n.startswith("ppt/") for n in names):
                return "pptx"
    except zipfile.BadZipFile:
        return "unknown"
    return "unknown"


def detect_type(path: Path) -> str:
    head = path.read_bytes()[:4096]
    suffix = path.suffix.lower()
    if head.startswith(b"%PDF") or b"%PDF-" in head[:1024]:
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return _zip_type(path)
    for magic, kind in _IMAGE_MAGIC:
        if head.startswith(magic):
            return kind
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image"
    if head[4:12] in (b"ftypavif", b"ftypheic", b"ftypheix", b"ftypmif1"):
        return "image"
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        # A multi-byte character may be cut at the 4 KiB boundary.
        try:
            text = head[:-4].decode("utf-8")
        except UnicodeDecodeError:
            return "unknown"
    lowered = text.lstrip("﻿ \t\r\n").lower()
    if suffix == ".svg" or lowered.startswith("<svg") or "<svg" in lowered[:400]:
        return "svg"
    if lowered.startswith(("<!doctype html", "<html")) or suffix in (".html", ".htm"):
        return "html"
    if suffix == ".eml" or any(lowered.startswith(h) for h in ("received:", "from:",
                                                                "return-path:", "mime-version:")):
        return "eml"
    if suffix in (".md", ".markdown"):
        return "md"
    return "txt"
