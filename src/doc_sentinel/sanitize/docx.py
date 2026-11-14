"""DOCX sanitizer using exact XML byte spans and mandatory render comparison.

Word run properties follow document defaults, paragraph style ancestry,
character style ancestry, paragraph run properties and direct run properties.
Colors use the same contrast and CIE76 thresholds as subtext-scan. Unsupported
or ambiguous locations are refused; the caller must supply complete fragments,
not finding excerpts. No document annotations or harness metadata are added.
"""
from __future__ import annotations

import math
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

from doc_sentinel.adapters.base import AdapterError, RunContext, run_tool
from doc_sentinel.sanitize.docx_xml import Atom, Node, attribute_atoms, parse_xml
from doc_sentinel.sanitize.docx_zip import read_package, write_package
from doc_sentinel.sanitize.text import invisible_offsets

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
REL = "http://schemas.openxmlformats.org/package/2006/relationships}"
PROPS = {"docProps/core.xml", "docProps/app.xml", "docProps/custom.xml"}
BODY = re.compile(r"word/(?:document|header\d*|footer\d*|footnotes|endnotes)\.xml\Z")
COMMENTS = re.compile(r"word/comments\d*\.xml\Z")
_TEXT = {W + "t", W + "delText"}
_BREAKS = {W + "tab": "\t", W + "br": "\n", W + "cr": "\n"}
_NAMED = {"white": "ffffff", "black": "000000", "yellow": "ffff00", "green": "008000",
          "cyan": "00ffff", "magenta": "ff00ff", "blue": "0000ff", "red": "ff0000",
          "darkblue": "00008b", "darkcyan": "008b8b", "darkgreen": "006400",
          "darkmagenta": "8b008b", "darkred": "8b0000", "darkyellow": "808000",
          "darkgray": "a9a9a9", "lightgray": "d3d3d3"}


@dataclass(frozen=True)
class Patch:
    part: str
    start: int
    end: int
    text: str
    technique: str
    visibility: str
    reason: str
    on_page: bool = False
    record: bool = True
    action_end: int | None = None
    extra_bytes: int = 0


@dataclass
class Stream:
    part: str
    channel: str
    pointer: str
    atoms: list[Atom]
    on_page: bool = False


@dataclass
class Outcome:
    status: str
    data: bytes | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    reason: str | None = None


def _check(status: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "detail": detail, **extra}


def _text(node: Node) -> str:
    return "".join(a.char for current in node.walk() for a in current.atoms)


def _rpr(node: Node | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if node is None:
        return result
    for child in node.children:
        name = child.tag.removeprefix(W)
        value = child.attrs.get(W + "val")
        if name in {"vanish", "specVanish", "webHidden"}:
            result[name] = (value or "true").lower() not in {"0", "false", "off"}
        elif name == "sz" and value:
            try:
                result["size"] = float(value)
            except ValueError as exc:
                raise ValueError("invalid DOCX font size") from exc
        elif name == "color" and value:
            result["color"] = value
        elif name == "highlight" and value:
            result["highlight"] = None if value.lower() == "none" else value
        elif name == "shd" and child.attrs.get(W + "fill"):
            value = child.attrs[W + "fill"]
            result["shading"] = None if value.lower() == "auto" else value
    return result


def _child(node: Node | None, tag: str) -> Node | None:
    return node.child(tag) if node is not None else None


def _val(node: Node | None) -> str | None:
    return node.attrs.get(W + "val") if node else None


def _styles(root: Node | None):
    if root is None:
        return {}, {}, None
    defaults = _rpr(_child(_child(_child(root, W + "docDefaults"), W + "rPrDefault"), W + "rPr"))
    styles: dict[str, tuple[str | None, dict[str, Any]]] = {}
    default_para = None
    for node in root.walk():
        if node.tag != W + "style":
            continue
        style_id = node.attrs.get(W + "styleId")
        if not style_id:
            continue
        if style_id in styles:
            raise ValueError("duplicate DOCX style ID")
        styles[style_id] = (_val(node.child(W + "basedOn")), _rpr(node.child(W + "rPr")))
        if node.attrs.get(W + "type") == "paragraph" and node.attrs.get(W + "default") in {
                "1", "true", "on"}:
            default_para = style_id
    return defaults, styles, default_para


def _chain(styles, style_id):
    chain, seen = [], set()
    while style_id and style_id in styles:
        if style_id in seen:
            raise ValueError("cyclic DOCX style inheritance")
        seen.add(style_id)
        parent, props = styles[style_id]
        chain.append(props)
        style_id = parent
    result = {}
    for props in reversed(chain):
        result.update(props)
    return result


def _rgb(value: str | None):
    if not value:
        return None
    value = _NAMED.get(value.lower(), value).lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        return None
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _near(a, b) -> bool:
    def lin(channel):
        value = channel / 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    def lab(rgb):
        r, g, b = (lin(c) for c in rgb)
        xyz = ((0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047,
               0.2126 * r + 0.7152 * g + 0.0722 * b,
               (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883)
        x, y, z = (v ** (1 / 3) if v > 216 / 24389 else (24389 / 27 * v + 16) / 116
                   for v in xyz)
        return 116 * y - 16, 500 * (x - y), 200 * (y - z)

    def luminance(rgb):
        r, g, b = (lin(c) for c in rgb)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    contrast = round((high + 0.05) / (low + 0.05), 2)
    delta = round(math.dist(lab(a), lab(b)), 1)
    return contrast < 1.25 and delta < 20


def _fill(node: Node | None):
    shd = _child(node, W + "shd")
    return shd.attrs.get(W + "fill") if shd else None


def _hidden(run: Node, defaults, styles, default_para, background) -> str | None:
    paragraph = run.ancestor(W + "p")
    if paragraph is None:
        return None
    ppr = paragraph.child(W + "pPr")
    rpr = run.child(W + "rPr")
    pstyle = _val(_child(ppr, W + "pStyle")) or default_para
    rstyle = _val(_child(rpr, W + "rStyle"))
    props = dict(defaults)
    props.update(_chain(styles, pstyle))
    props.update(_chain(styles, rstyle))
    props.update(_rpr(_child(ppr, W + "rPr")))
    props.update(_rpr(rpr))
    flags = [name for name in ("vanish", "specVanish", "webHidden") if props.get(name)]
    if flags:
        return ", ".join(flags)
    if props.get("size") is not None and props["size"] < 8:
        return "font smaller than 4 pt"
    foreground = _rgb(props.get("color"))
    cell = paragraph.ancestor(W + "tc")
    cell_pr = _child(cell, W + "tcPr")
    bg = (_rgb(props.get("highlight")) or _rgb(props.get("shading"))
          or _rgb(_fill(ppr)) or _rgb(_fill(cell_pr)) or background or (255, 255, 255))
    if foreground is not None and _near(foreground, bg):
        return "low-contrast foreground against effective background"
    return None


def _raw_roots(entries):
    names = [name for name in entries if name in PROPS or
             (name.startswith("word/") and name.endswith((".xml", ".rels")))]
    return {name: parse_xml(entries[name]) for name in names}


def _node_atoms(node: Node) -> list[Atom]:
    if node.tag in _TEXT:
        if node.children:
            raise ValueError("nested XML inside Word text is unsupported")
        return node.atoms
    if node.tag in _BREAKS:
        return [Atom(_BREAKS[node.tag], node.start, node.end)]
    return []


def _structural(roots):
    defaults, styles, default_para = _styles(roots.get("word/styles.xml"))
    document = roots["word/document.xml"]
    bg_node = next((n for n in document.walk() if n.tag == W + "background"), None)
    background = _rgb(bg_node.attrs.get(W + "color")) if bg_node else None
    patches: list[Patch] = []
    for name, root in roots.items():
        if not (BODY.fullmatch(name) or COMMENTS.fullmatch(name)):
            continue
        for node in root.walk():
            if node.tag == W + "del" and node.ancestor(W + "del") is None:
                atoms = [a for child in node.walk() for a in _node_atoms(child)]
                patches.append(Patch(name, node.start, node.end, "".join(a.char for a in atoms),
                                     "tracked_deletion", "hidden", "deleted in the final view"))
            elif node.tag == W + "ins" and node.ancestor(W + "del") is None:
                # Remove only the wrapper, never the inserted runs. Keep these two
                # byte edits distinct so nested hidden/visible edits cannot overlap.
                tail = node.end - node.closing_start
                patches.append(Patch(name, node.start, node.opening_end, "",
                                     "tracked_insertion_accepted", "visible",
                                     "insertion accepted in the final view",
                                     action_end=node.end, extra_bytes=tail))
                if tail:
                    patches.append(Patch(name, node.closing_start, node.end, "",
                                         "tracked_insertion_accepted", "visible",
                                         "insertion accepted in the final view", record=False))
            elif node.tag == W + "r" and node.ancestor(W + "del") is None:
                reason = _hidden(node, defaults, styles, default_para, background)
                if reason:
                    for child in node.children:
                        if child.tag in _TEXT or child.tag in _BREAKS:
                            if child.tag == W + "delText":
                                continue
                            atoms = _node_atoms(child)
                            patches.append(Patch(name, child.start, child.end,
                                                 "".join(a.char for a in atoms), "hidden-run",
                                                 "hidden", reason))
    return patches


def _covered(part: str, atom: Atom, patches: list[Patch]) -> bool:
    return any(p.part == part and p.start <= atom.start and atom.end <= p.end for p in patches)


def _streams(roots, entries, patches):
    result: list[Stream] = []
    for name, root in roots.items():
        if BODY.fullmatch(name) or COMMENTS.fullmatch(name):
            for para in (node for node in root.walk() if node.tag == W + "p"):
                comment = para.ancestor(W + "comment") if COMMENTS.fullmatch(name) else None
                if COMMENTS.fullmatch(name) and comment is None:
                    continue
                channel = "comment" if comment else "body"
                pointer = f"{name}#{comment.attrs.get(W + 'id', '')}" if comment else name
                groups: list[list[Atom]] = [[]]
                for run in para.walk():
                    if run.tag != W + "r" or run.ancestor(W + "p") is not para:
                        continue
                    if run.ancestor(W + "del") is not None or run.ancestor(W + "fldSimple") is not None:
                        if groups[-1]:
                            groups.append([])
                        continue
                    for child in run.children:
                        if child.tag == W + "rPr":
                            continue
                        if child.tag == W + "t" or child.tag in _BREAKS:
                            groups[-1].extend(a for a in _node_atoms(child)
                                              if not _covered(name, a, patches))
                        elif groups[-1]:
                            # Drawings, field controls and references separate runs;
                            # an exact visible span must never cross them.
                            groups.append([])
                result.extend(Stream(name, channel, pointer, atoms, channel == "body")
                              for atoms in groups if atoms)
        if name.startswith("word/") and name.endswith(".xml"):
            for node in root.walk():
                if node.tag != W + "t":
                    continue
                para = node.ancestor(W + "p")
                run = node.ancestor(W + "r")
                scoped = (run is not None and run.ancestor(W + "fldSimple") is None and
                          ((BODY.fullmatch(name) and para is not None) or
                           (COMMENTS.fullmatch(name) and para is not None
                            and para.ancestor(W + "comment") is not None)))
                if not scoped:
                    atoms = [a for a in _node_atoms(node) if not _covered(name, a, patches)]
                    result.append(Stream(name, "unscoped", name, atoms))
        if name in PROPS:
            for node in root.children:
                pointer = node.attrs.get("name") if node.tag.endswith("}property") else node.tag.split("}")[-1]
                atoms = [a for child in node.walk() for a in child.atoms]
                result.append(Stream(name, "metadata", pointer or "", atoms))
        if name.startswith("word/") and name.endswith(".xml"):
            for node in root.walk():
                if node.tag == WP + "docPr":
                    for attr in ("descr", "title"):
                        if attr in node.attrs:
                            result.append(Stream(name, "alt", f"{name}#docPr@{attr}",
                                                 attribute_atoms(entries[name], node, attr)))
        if name.startswith("word/") and name.endswith(".rels"):
            for node in root.walk():
                if (node.tag == REL + "Relationship" and node.attrs.get("TargetMode", "").lower() == "external"
                        and node.attrs.get("Type", "").endswith("/hyperlink") and "Target" in node.attrs):
                    result.append(Stream(name, "hyperlink", f"{name}#{node.attrs.get('Id', '')}",
                                         attribute_atoms(entries[name], node, "Target")))
    return result


def _match_pointer(span, stream: Stream) -> bool:
    pointer = span.get("pointer")
    return (span.get("channel") == stream.channel and
            (pointer is None or pointer == stream.pointer or
             (stream.channel == "body" and pointer == "body")))


def _atom_patches(part, atoms, technique, visibility, reason, *, on_page=False):
    if not atoms:
        return []
    patches = []
    group = [atoms[0]]
    for atom in atoms[1:]:
        if atom.start == group[-1].end or (atom.start == group[-1].start and atom.end == group[-1].end):
            group.append(atom)
        else:
            patches.append(Patch(part, group[0].start, group[-1].end,
                                 "".join(a.char for a in group), technique, visibility, reason,
                                 on_page=on_page))
            group = [atom]
    patches.append(Patch(part, group[0].start, group[-1].end,
                         "".join(a.char for a in group), technique, visibility, reason,
                         on_page=on_page))
    return patches


def _plan(roots, entries, spans):
    patches = _structural(roots)
    streams = _streams(roots, entries, patches)
    visible_texts: list[str] = []
    for span in spans:
        if not isinstance(span, dict):
            raise TypeError("injection span must be an object")
        text = span.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("injection span text must be nonempty")
        hits = []
        for stream in streams:
            if not _match_pointer(span, stream):
                continue
            source = "".join(atom.char for atom in stream.atoms)
            start = source.find(text)
            while start >= 0:
                hits.append((stream, start))
                start = source.find(text, start + 1)
        if len(hits) != 1:
            raise ValueError("DOCX injection span is missing or ambiguous")
        stream, start = hits[0]
        selected = stream.atoms[start:start + len(text)]
        if any(_covered(stream.part, atom, patches) for atom in selected):
            raise ValueError("DOCX injection spans overlap")
        patches.extend(_atom_patches(stream.part, selected, "visible-injection", "visible",
                                     "caller supplied an exact injection fragment",
                                     on_page=stream.on_page))
        if stream.on_page:
            visible_texts.append(text)
    # Apply the shared Unicode rules to paragraph text, retaining context across runs.
    for stream in streams:
        if stream.channel not in {"body", "comment", "unscoped"}:
            continue
        atoms = [a for a in stream.atoms if not _covered(stream.part, a, patches)]
        text = "".join(atom.char for atom in atoms)
        selected = [atoms[i] for i in invisible_offsets(text, leading_bom=False)]
        patches.extend(_atom_patches(stream.part, selected, "invisible-unicode", "hidden",
                                     "format character creates a view/extraction asymmetry"))
    patches.sort(key=lambda item: (item.part, item.start))
    if any(a.part == b.part and a.end > b.start for a, b in pairwise(patches)):
        raise ValueError("overlapping DOCX edits")
    return patches, visible_texts


def _apply(entries, patches):
    result = dict(entries)
    for part in {p.part for p in patches}:
        raw = entries[part]
        pieces, position = [], 0
        for patch in (item for item in patches if item.part == part):
            pieces.append(raw[position:patch.start])
            position = patch.end
        pieces.append(raw[position:])
        result[part] = b"".join(pieces)
    return result


def _extractions(roots, entries):
    result = {}
    for name, root in roots.items():
        atoms: list[Atom] = []
        if BODY.fullmatch(name) or COMMENTS.fullmatch(name):
            atoms.extend(a for node in root.walk() for a in _node_atoms(node))
        elif name.startswith("word/") and name.endswith(".xml"):
            atoms.extend(a for node in root.walk() if node.tag == W + "t" for a in _node_atoms(node))
        if name in PROPS:
            atoms.extend(a for node in root.walk() for a in node.atoms)
        if name.startswith("word/") and name.endswith(".xml"):
            for node in root.walk():
                if node.tag == WP + "docPr":
                    for attr in ("descr", "title"):
                        if attr in node.attrs:
                            atoms.extend(attribute_atoms(entries[name], node, attr))
        if name.startswith("word/") and name.endswith(".rels"):
            for node in root.walk():
                if (node.tag == REL + "Relationship" and node.attrs.get("TargetMode", "").lower() == "external"
                        and node.attrs.get("Type", "").endswith("/hyperlink") and "Target" in node.attrs):
                    atoms.extend(attribute_atoms(entries[name], node, "Target"))
        result[name] = atoms
    return result


def _delta_ok(original, changed, patches):
    for part, atoms in original.items():
        expected = "".join(a.char for a in atoms if not _covered(part, a, patches))
        actual = "".join(a.char for a in changed.get(part, []))
        if " ".join(expected.split()) != " ".join(actual.split()):
            return False
    return True


def _actions(patches):
    return [{"format": "docx", "part": p.part,
             "location": {"start_byte": p.start, "end_byte": p.action_end or p.end},
             "technique": p.technique, "visibility": p.visibility, "reason": p.reason,
             "removed_text": p.text, "bytes_removed": p.end - p.start + p.extra_bytes}
            for p in patches if p.record]


def render_equal(src: Path, candidate: Path, visible: list[str], workdir: Path, *,
                 reference: Path | None = None):
    """Compare with a final-view reference containing only the visible removals.

    The original final view supplies affected regions. A second reference with
    precisely the authorized visible edits accounts for natural text reflow;
    every pixel of that reference must equal the delivered candidate.
    """
    executable = shutil.which("soffice")
    if executable is None:
        return False, [], "LibreOffice is unavailable"
    try:
        import pymupdf

        with tempfile.TemporaryDirectory(prefix="doc-sentinel-render-", dir=workdir) as scratch:
            root = Path(scratch)
            pdfs = []
            paths = (src, reference, candidate) if reference else (src, candidate)
            for index, path in enumerate(paths):
                directory = root / str(index)
                directory.mkdir()
                source = directory / "document.docx"
                source.write_bytes(path.read_bytes())
                profile = (root / f"profile{index}").as_uri()
                ctx = RunContext(workdir=root, timeout_s=120)
                run_tool([executable, f"-env:UserInstallation={profile}", "--headless",
                          "--convert-to", "pdf", "--outdir", str(directory), str(source)], ctx,
                         extra_env={"XDG_CONFIG_HOME": str(root / "config"),
                                    "XDG_CACHE_HOME": str(root / "cache")}, ok_codes=(0,))
                pdf = directory / "document.pdf"
                if not pdf.is_file():
                    return False, [], "LibreOffice did not produce a PDF"
                pdfs.append(pdf)
            with (pymupdf.open(pdfs[0]) as before,
                  pymupdf.open(pdfs[-2]) as expected,
                  pymupdf.open(pdfs[-1]) as after):
                if len(before) != len(expected) or len(expected) != len(after):
                    return False, [], "page count changed"
                regions = []
                for text in visible:
                    located = [(page.number, rect) for page in before for rect in page.search_for(text)]
                    if not located:
                        return False, regions, "visible injection could not be located in render"
                    regions.extend({"page": number + 1, "rect": list(rect)} for number, rect in located)
                scale = 1.5
                for page_num, (old_page, ref_page, new_page) in enumerate(zip(before, expected, after), 1):
                    images = [page.get_pixmap(matrix=pymupdf.Matrix(scale, scale),
                                              colorspace=pymupdf.csRGB, alpha=False)
                              for page in (old_page, ref_page, new_page)]
                    old, ref, new = images
                    if any((image.width, image.height, image.n) != (old.width, old.height, old.n)
                           for image in images[1:]):
                        return False, regions, "page dimensions changed"
                    if ref.samples != new.samples:
                        return False, regions, "pixels changed outside visible injection edits"
                    if reference and old.samples != ref.samples:
                        # Reflow may affect pixels beyond the original sentence glyphs.
                        min_x, min_y, max_x, max_y = old.width, old.height, -1, -1
                        old_samples, ref_samples = old.samples, ref.samples
                        for index in range(old.width * old.height):
                            pos = index * old.n
                            if old_samples[pos:pos + old.n] != ref_samples[pos:pos + old.n]:
                                x, y = index % old.width, index // old.width
                                min_x, min_y = min(min_x, x), min(min_y, y)
                                max_x, max_y = max(max_x, x), max(max_y, y)
                        if max_x >= 0:
                            regions.append({"page": page_num, "rect": [min_x / scale, min_y / scale,
                                            (max_x + 1) / scale, (max_y + 1) / scale]})
            return True, regions, "all pages compared at 108 DPI against final view"
    except (OSError, ImportError, ValueError, AdapterError):
        return False, [], "LibreOffice rendering or offline isolation failed"


def sanitize_docx(src: Path, spans: list[dict], workdir: Path) -> Outcome:
    infos, entries, comment, raw, start_dir = read_package(src)
    roots = _raw_roots(entries)
    patches, visible = _plan(roots, entries, spans)
    if not patches:
        return Outcome("nothing_to_do", checks={
            "redetect_clean": _check("passed", "no supported removals found"),
            "opens": _check("skipped", "no copy produced"),
            "render_equal": _check("skipped", "no copy produced"),
            "extraction_delta_ok": _check("skipped", "no copy produced"),
        })
    changed = _apply(entries, patches)
    new_roots = _raw_roots(changed)
    delta_ok = _delta_ok(_extractions(roots, entries), _extractions(new_roots, changed), patches)
    remaining, _ = _plan(new_roots, changed, [])
    streams = _streams(new_roots, changed, [])
    still_present = any(any(_match_pointer(span, s) and span["text"] in "".join(a.char for a in s.atoms)
                            for s in streams) for span in spans)
    data = write_package(infos, changed, {p.part for p in patches}, comment, raw, start_dir)
    # Re-open the actual packaged bytes, not just the in-memory XML parts.
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if archive.testzip() is not None:
            raise ValueError("sanitized ZIP failed CRC verification")
        for part in {p.part for p in patches}:
            parse_xml(archive.read(part))
    actions = _actions(patches)
    checks = {
        "opens": _check("passed", "ZIP CRC and edited XML parts verified"),
        "extraction_delta_ok": _check("passed" if delta_ok else "failed", "compared XML text by part"),
        "redetect_clean": _check("passed" if not remaining and not still_present else "failed",
                                 "rescanned supported DOCX rules and supplied fragments"),
    }
    if not delta_ok or remaining or still_present:
        checks["render_equal"] = _check("skipped", "earlier self-check failed")
        return Outcome("refused", actions=actions, checks=checks, reason="DOCX text self-check failed")
    with tempfile.TemporaryDirectory(prefix="doc-sentinel-candidate-", dir=workdir) as scratch:
        root = Path(scratch)
        candidate = root / "candidate.docx"
        candidate.write_bytes(data)
        final_patches = [p for p in patches if p.technique in {
            "tracked_deletion", "tracked_insertion_accepted"}]
        final_view = src
        if final_patches:
            final_view = root / "final-view.docx"
            final_view.write_bytes(write_package(infos, _apply(entries, final_patches),
                                                 {p.part for p in final_patches}, comment, raw, start_dir))
        reference = None
        if visible:
            # Start from the original bytes and apply only final-view changes and
            # explicitly authorized on-page injections, not hidden-content edits.
            reference_patches = [p for p in patches if p in final_patches or p.on_page]
            reference = root / "visible-edits.docx"
            reference.write_bytes(write_package(infos, _apply(entries, reference_patches),
                                                {p.part for p in reference_patches},
                                                comment, raw, start_dir))
        equal, regions, reason = render_equal(final_view, candidate, visible, workdir, reference=reference)
    checks["render_equal"] = _check("passed" if equal else "failed", reason, regions=regions)
    if not equal:
        return Outcome("refused", actions=actions, checks=checks, reason=reason)
    return Outcome("sanitized", data, actions, checks)
