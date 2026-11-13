"""Remove unreferenced Word HTML-paste residue without rewriting other parts."""
from __future__ import annotations

import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.parsers import expat

from doc_sentinel.adapters.base import Capability, CleanResult, RunContext
from doc_sentinel.schema import Finding, Location

_WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/main}"
_MAX_UNCOMPRESSED = 128 * 1024 * 1024
_CSS_STYLE = re.compile(r"[a-z0-9_-]+\Z")
_STYLE_REFS = {_WORD + name for name in ("pStyle", "rStyle", "tblStyle", "numStyleId")}
_STYLE_LINKS = {_WORD + name for name in ("basedOn", "next", "link")}
_TEXT_TAGS = {_WORD + name for name in ("t", "delText", "instrText")} | {_DRAWING + "t"}


@dataclass
class _Scan:
    refs: set[str] = field(default_factory=set)
    has_div_ref: bool = False
    styles: list[tuple[str, int, int]] = field(default_factory=list)
    divs: list[tuple[int, int, int]] = field(default_factory=list)
    visible: list[str] = field(default_factory=list)


def _tag_end(raw: bytes, index: int, start: int) -> int:
    # Expat reports the next token's offset for empty elements. That token can
    # itself be the parent's closing tag, so inspect the original opening tag.
    quote = None
    for pos in range(start + 1, len(raw)):
        char = raw[pos]
        if quote is not None:
            if char == quote:
                quote = None
        elif char in (ord('"'), ord("'")):
            quote = char
        elif char == ord(">"):
            if raw[pos - 1] == ord("/"):
                return index
            break
    if raw[index:index + 2] != b"</":
        raise ValueError("invalid closing XML tag")
    end = raw.find(b">", index)
    if end < 0:
        raise ValueError("invalid closing XML tag")
    return end + 1


def _scan(raw: bytes, name: str) -> _Scan:
    result = _Scan()
    parser = expat.ParserCreate(namespace_separator="}")
    stack: list[dict] = []

    def reject(*_args):
        raise ValueError("DOCTYPE and external entities are not allowed")

    def start(tag, attrs):
        node = {"tag": tag, "start": parser.CurrentByteIndex}
        if tag in _STYLE_REFS or (name == "word/styles.xml" and tag in _STYLE_LINKS):
            val = attrs.get(_WORD + "val")
            if val:
                result.refs.add(val)
        if tag == _WORD + "divId":
            result.has_div_ref = True
        if name == "word/styles.xml" and tag == _WORD + "style":
            node.update(style_id=attrs.get(_WORD + "styleId"),
                        custom=attrs.get(_WORD + "customStyle"), style_name=None)
        if tag == _WORD + "name" and stack and stack[-1]["tag"] == _WORD + "style":
            stack[-1]["style_name"] = attrs.get(_WORD + "val")
        if name == "word/webSettings.xml" and tag == _WORD + "divs":
            node["div_count"] = 0
        if tag == _WORD + "div":
            for parent in reversed(stack):
                if parent["tag"] == _WORD + "divs":
                    parent["div_count"] += 1
                    break
        stack.append(node)

    def end(tag):
        node = stack.pop()
        finish = _tag_end(raw, parser.CurrentByteIndex, node["start"])
        if tag == _WORD + "style" and name == "word/styles.xml":
            style_id = node.get("style_id")
            if (node.get("custom") == "1" and style_id and style_id == node.get("style_name")
                    and _CSS_STYLE.fullmatch(style_id)):
                result.styles.append((style_id, node["start"], finish))
        if tag == _WORD + "divs" and name == "word/webSettings.xml":
            result.divs.append((node["start"], finish, node["div_count"]))

    def text(data):
        if stack and stack[-1]["tag"] in _TEXT_TAGS:
            result.visible.append(data)

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = text
    parser.StartDoctypeDeclHandler = reject
    parser.ExternalEntityRefHandler = reject
    try:
        parser.Parse(raw, True)
    except expat.ExpatError as exc:
        raise ValueError(f"invalid XML in {name}: {exc}") from exc
    return result


def _read_archive(src: Path):
    with zipfile.ZipFile(src) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise ValueError("duplicate ZIP entries are not supported")
        if sum(info.file_size for info in infos) > _MAX_UNCOMPRESSED:
            raise ValueError("ZIP uncompressed size exceeds safety limit")
        entries: dict[str, bytes] = {}
        remaining = _MAX_UNCOMPRESSED
        for info in infos:
            with archive.open(info) as member:
                data = member.read(remaining + 1)
            if len(data) > remaining:
                raise ValueError("ZIP uncompressed size exceeds safety limit")
            remaining -= len(data)
            entries[info.filename] = data
        return infos, entries, archive.comment


def _analyse(entries: dict[str, bytes]):
    scans = {name: _scan(data, name) for name, data in entries.items()
             if name.startswith("word/") and name.endswith(".xml")}
    refs = set().union(*(scan.refs for scan in scans.values()))
    has_div_ref = any(scan.has_div_ref for scan in scans.values())
    candidates = scans.get("word/styles.xml", _Scan()).styles
    styles = [(name, start, end) for name, start, end in candidates if name not in refs]
    divs = [] if has_div_ref else scans.get("word/webSettings.xml", _Scan()).divs
    return scans, styles, divs


def _without_spans(raw: bytes, spans: list[tuple[int, int]]) -> bytes:
    for start, end in sorted(spans, reverse=True):
        raw = raw[:start] + raw[end:]
    return raw


class OoxmlPasteResidueCleaner:
    id = "ooxml-paste-residue"
    mode = frozenset({"clean"})

    def available(self) -> Capability:
        return Capability(True)

    def inspect(self, src: Path, ctx: RunContext) -> list[Finding]:
        if Path(src).suffix.lower() != ".docx":
            return []
        _, entries, _ = _read_archive(Path(src))
        if "word/document.xml" not in entries:
            return []
        _, styles, divs = _analyse(entries)
        findings = []
        for code, count in (("ORPHAN_STYLES", len(styles)),
                            ("ORPHAN_DIVS", sum(item[2] for item in divs))):
            if count:
                findings.append(Finding(adapter=self.id, adapter_code=code,
                                        cls="document_metadata", severity="info",
                                        confidence="measured",
                                        location=Location(file=str(src), channel="metadata"),
                                        evidence={"count": count}))
        return findings

    def clean(self, src: Path, dest: Path, ctx: RunContext) -> CleanResult:
        src, dest = Path(src), Path(dest)
        if src.resolve() == dest.resolve() or (dest.exists() and src.samefile(dest)):
            raise ValueError("output path must differ from input")
        if src.suffix.lower() != ".docx":
            shutil.copyfile(src, dest)
            return CleanResult(changed=False)
        infos, entries, comment = _read_archive(src)
        if "word/document.xml" not in entries:
            shutil.copyfile(src, dest)
            return CleanResult(changed=False)
        scans, styles, divs = _analyse(entries)
        actions = []
        if styles:
            entries["word/styles.xml"] = _without_spans(entries["word/styles.xml"],
                                                         [(s, e) for _, s, e in styles])
            actions.append({"action": "removed", "what": "orphan HTML-paste styles",
                            "count": len(styles), "names": [name for name, _, _ in styles]})
        if divs:
            entries["word/webSettings.xml"] = _without_spans(entries["word/webSettings.xml"],
                                                              [(s, e) for s, e, _ in divs])
            actions.append({"action": "removed", "what": "unreferenced HTML-paste divs",
                            "count": sum(count for _, _, count in divs)})
        if not actions:
            shutil.copyfile(src, dest)
            return CleanResult(changed=False)
        after, _, _ = _analyse(entries)
        for name, scan in scans.items():
            if "".join(scan.visible) != "".join(after[name].visible):
                raise ValueError(f"visible text changed in {name}")
        with zipfile.ZipFile(dest, "w") as archive:
            archive.comment = comment
            for info in infos:
                archive.writestr(info, entries[info.filename])
        return CleanResult(changed=True, actions=actions)
