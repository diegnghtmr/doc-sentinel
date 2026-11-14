"""Preserve unchanged local ZIP records, including compressed bytes, in order."""
from __future__ import annotations

import copy
import io
import zipfile
from pathlib import Path

from doc_sentinel.glue.ooxml_paste import _read_archive


def read_package(src: Path):
    if src.stat().st_size > 200 * 1024 * 1024:
        raise ValueError("DOCX archive exceeds size limit")
    infos, entries, comment = _read_archive(src)
    if len(infos) > 10_000:
        raise ValueError("DOCX archive contains too many entries")
    if any(info.flag_bits & 1 for info in infos):
        raise ValueError("encrypted DOCX entries are unsupported")
    if "word/document.xml" not in entries:
        raise ValueError("DOCX document part is missing")
    raw = src.read_bytes()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if [i.header_offset for i in infos] != sorted(i.header_offset for i in infos):
            raise ValueError("ZIP central and local entry order differs")
        end = archive.start_dir
    return infos, entries, comment, raw, end


def write_package(infos, entries: dict[str, bytes], changed: set[str], comment: bytes,
                  raw: bytes, start_dir: int) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.comment = comment
        if infos:
            archive.fp.write(raw[:infos[0].header_offset])
            archive.start_dir = archive.fp.tell()
        for index, info in enumerate(infos):
            if info.filename in changed:
                archive.writestr(copy.copy(info), entries[info.filename])
                continue
            end = infos[index + 1].header_offset if index + 1 < len(infos) else start_dir
            if not 0 <= info.header_offset <= end <= start_dir:
                raise ValueError("invalid ZIP entry offsets")
            cloned = copy.copy(info)
            cloned.header_offset = archive.fp.tell()
            archive.fp.write(raw[info.header_offset:end])
            archive.filelist.append(cloned)
            archive.NameToInfo[cloned.filename] = cloned
            archive.start_dir = archive.fp.tell()
    return out.getvalue()
