"""Remove the observed python-docx template date and thumbnail fingerprints."""
from __future__ import annotations

import hashlib
import posixpath
import shutil
import zipfile
from pathlib import Path
from urllib.parse import unquote
from xml.parsers import expat

from doc_sentinel.adapters.base import Capability, CleanResult, RunContext
from doc_sentinel.glue.ooxml_paste import _analyse, _read_archive, _tag_end, _without_spans
from doc_sentinel.schema import Finding, Location

_DATE_NS = "http://purl.org/dc/terms/}"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships}"
_TYPE_NS = "http://schemas.openxmlformats.org/package/2006/content-types}"
_DATE = "2013-12-23T23:15:00Z"
_THUMBNAIL = "docProps/thumbnail.jpeg"
# SHA-256 of the unmodified thumbnail in the generated python-docx default template.
_THUMBNAIL_SHA256 = "96367138dc44ce09bf2c8f0f8e49348a1478d2c5c0af69bbc2bbc38b63cdcead"


def _xml_records(raw: bytes, wanted: set[str]):
    """Return element byte spans, attributes and text without serializing XML."""
    parser = expat.ParserCreate(namespace_separator="}")
    stack: list[dict] = []
    records: list[dict] = []

    def reject(*_args):
        raise ValueError("DOCTYPE and external entities are not allowed")

    def start(tag, attrs):
        stack.append({"tag": tag, "attrs": attrs, "start": parser.CurrentByteIndex,
                      "text": []})

    def text(data):
        if stack:
            stack[-1]["text"].append(data)

    def end(tag):
        node = stack.pop()
        if tag in wanted:
            node["end"] = _tag_end(raw, parser.CurrentByteIndex, node["start"])
            node["value"] = "".join(node["text"])
            records.append(node)

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = text
    parser.StartDoctypeDeclHandler = reject
    parser.ExternalEntityRefHandler = reject
    try:
        parser.Parse(raw, True)
    except expat.ExpatError as exc:
        raise ValueError(f"invalid OOXML metadata: {exc}") from exc
    return records


def _date_records(entries):
    core = entries.get("docProps/core.xml")
    if core is None:
        return []
    records = _xml_records(core, {_DATE_NS + "created", _DATE_NS + "modified"})
    # Both dates form the fingerprint; do not erase a standalone user date.
    if (len(records) == 2 and {r["tag"] for r in records} ==
            {_DATE_NS + "created", _DATE_NS + "modified"}
            and all(r["value"] == _DATE for r in records)):
        return records
    return []


def _thumbnail_refs(entries):
    removals: dict[str, list[tuple[int, int]]] = {}
    for name, raw in entries.items():
        if not name.endswith(".rels"):
            continue
        base = posixpath.dirname(posixpath.dirname(name))
        for record in _xml_records(raw, {_REL_NS + "Relationship"}):
            attrs = record["attrs"]
            if attrs.get("TargetMode", "").lower() == "external":
                continue
            target = unquote(attrs.get("Target", "")).split("#", 1)[0]
            resolved = posixpath.normpath(target.lstrip("/") if target.startswith("/")
                                         else posixpath.join(base, target))
            if resolved != _THUMBNAIL:
                continue
            if not attrs.get("Type", "").endswith("/thumbnail"):
                return None  # another relationship uses the image as content
            removals.setdefault(name, []).append((record["start"], record["end"]))
    types = entries.get("[Content_Types].xml")
    if types is not None:
        for record in _xml_records(types, {_TYPE_NS + "Override"}):
            if record["attrs"].get("PartName") == "/" + _THUMBNAIL:
                removals.setdefault("[Content_Types].xml", []).append(
                    (record["start"], record["end"]))
    return removals


def _candidates(entries):
    dates = _date_records(entries)
    thumbnail = entries.get(_THUMBNAIL)
    refs = None
    if thumbnail is not None and hashlib.sha256(thumbnail).hexdigest() == _THUMBNAIL_SHA256:
        refs = _thumbnail_refs(entries)
    return dates, refs


class OoxmlFingerprintCleaner:
    id = "ooxml-fingerprint"
    mode = frozenset({"clean"})

    def available(self) -> Capability:
        return Capability(True)

    def inspect(self, src: Path, ctx: RunContext) -> list[Finding]:
        if Path(src).suffix.lower() != ".docx":
            return []
        _, entries, _ = _read_archive(Path(src))
        if "word/document.xml" not in entries:
            return []
        dates, refs = _candidates(entries)
        findings = []
        for code, count in (("PYTHON_DOCX_DATES", len(dates)),
                            ("PYTHON_DOCX_THUMBNAIL", int(refs is not None))):
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
        before, _, _ = _analyse(entries)
        dates, refs = _candidates(entries)
        actions = []
        if dates:
            entries["docProps/core.xml"] = _without_spans(
                entries["docProps/core.xml"], [(r["start"], r["end"]) for r in dates])
            actions.append({"action": "removed", "what": "python-docx template dates",
                            "count": len(dates), "names": ["created", "modified"]})
        if refs is not None:
            del entries[_THUMBNAIL]
            for name, spans in refs.items():
                entries[name] = _without_spans(entries[name], spans)
            actions.append({"action": "removed", "what": "python-docx default thumbnail",
                            "count": 1, "names": [_THUMBNAIL]})
        if not actions:
            shutil.copyfile(src, dest)
            return CleanResult(changed=False)
        after, _, _ = _analyse(entries)
        for name, scan in before.items():
            if "".join(scan.visible) != "".join(after[name].visible):
                raise ValueError(f"visible text changed in {name}")
        with zipfile.ZipFile(dest, "w") as archive:
            archive.comment = comment
            for info in infos:
                if info.filename in entries:
                    archive.writestr(info, entries[info.filename])
        return CleanResult(changed=True, actions=actions)
