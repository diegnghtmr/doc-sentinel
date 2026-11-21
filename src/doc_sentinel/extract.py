"""Channel extractor ("pegamento propio", PLAN §2.2).

Produces the text channels that no sub-harness classifies on its own (filename, metadata,
comments, alt text, hyperlinks, embedded file names, form fields, tracked changes, OCR of
images, plain body text). The Unicode scanner and Prompt Guard consume these channels.

This module only *reads* the document. It never writes to it.
"""

from __future__ import annotations

from pathlib import Path

from doc_sentinel.schema import Channel

_TEXT_TYPES = {"txt", "md", "html", "svg", "eml"}
_MAX_TEXT = 2_000_000


def extract_channels(src: Path, dtype: str) -> list[Channel]:
    channels = [Channel("filename", src.name, pointer="filename")]
    if dtype in _TEXT_TYPES:
        raw = src.read_bytes()[:_MAX_TEXT]
        channels.append(Channel("body", raw.decode("utf-8", errors="replace"), pointer="body"))
    return channels
