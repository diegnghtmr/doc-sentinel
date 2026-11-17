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
from dataclasses import dataclass, field, replace
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
    replacement: bytes = b""
    spacer_width_pt: float = 0.0
    spacer_count: int = 0


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


def _run_properties(run: Node, defaults, styles, default_para) -> dict[str, Any]:
    paragraph = run.ancestor(W + "p")
    ppr = _child(paragraph, W + "pPr")
    rpr = run.child(W + "rPr")
    pstyle = _val(_child(ppr, W + "pStyle")) or default_para
    rstyle = _val(_child(rpr, W + "rStyle"))
    props = dict(defaults)
    props.update(_chain(styles, pstyle))
    props.update(_chain(styles, rstyle))
    props.update(_rpr(_child(ppr, W + "rPr")))
    props.update(_rpr(rpr))
    return props


def _hidden(run: Node, defaults, styles, default_para, background) -> str | None:
    paragraph = run.ancestor(W + "p")
    if paragraph is None:
        return None
    ppr = paragraph.child(W + "pPr")
    props = _run_properties(run, defaults, styles, default_para)
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
                            value = "".join(a.char for a in atoms)
                            # A single expanded U+0020 carries no hidden content.  It is
                            # the only whitespace-only run our layout preservation emits.
                            spacing = _child(node.child(W + "rPr"), W + "spacing")
                            if (child.tag == W + "t" and value == " " and spacing is not None
                                    and reason in {"font smaller than 4 pt",
                                                   "low-contrast foreground against effective background"}):
                                continue
                            patches.append(Patch(name, child.start, child.end, value, "hidden-run",
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


def reviewed_mixed_spans(src: Path, pointer: str | None, fragment: str):
    """Bind a reviewed body excerpt to XML atoms before excluding structural edits.

    Return None if it is not an unambiguous mixed/hidden excerpt; otherwise return
    exact visible instruction sentences within the reviewed part of the excerpt.
    """
    from doc_sentinel.glue.instruction_phrases import instruction_spans
    from doc_sentinel.sanitize.pdf_to_docx import _normalized_positions

    _infos, entries, _comment, _raw, _directory = read_package(src)
    roots = _raw_roots(entries)
    if pointer not in roots or not BODY.fullmatch(pointer):
        return None
    structural = _structural(roots)
    atoms = [atom for node in roots[pointer].walk() if node.tag == W + "t"
             for atom in _node_atoms(node)]
    text = "".join(atom.char for atom in atoms)
    normalized, positions = _normalized_positions(text)
    wanted, _ = _normalized_positions(fragment)
    start = normalized.find(wanted) if wanted else -1
    matches = []
    while start >= 0:
        matches.append(atoms[positions[start]:positions[start + len(wanted) - 1] + 1])
        start = normalized.find(wanted, start + 1)
    if not matches:
        return None
    if len(matches) > 1:
        # A duplicate is safe only when every occurrence is entirely removed by
        # the structural hidden-content plan; never discard a visible twin.
        return [] if all(all(_covered(pointer, atom, structural) for atom in match)
                         for match in matches) else None
    selected = matches[0]
    if not any(_covered(pointer, atom, structural) for atom in selected):
        return None
    selected_keys = {(atom.start, atom.end) for atom in selected}
    spans = []
    for stream in _streams(roots, entries, structural):
        if stream.channel != "body" or stream.part != pointer:
            continue
        visible = "".join(atom.char for atom in stream.atoms)
        reviewed = {index for index, atom in enumerate(stream.atoms)
                    if (atom.start, atom.end) in selected_keys}
        for left, right in instruction_spans(visible):
            if not all(index in reviewed for index in range(left, right)):
                continue
            from doc_sentinel.pipeline import _sentence_bounds

            begin, end = _sentence_bounds(visible, left, right)
            span = {"channel": "body", "pointer": pointer, "text": visible[begin:end]}
            if span not in spans:
                spans.append(span)
    return spans


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
            pieces.append(patch.replacement)
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
        additions = [(p.start, " " * p.spacer_count) for p in patches
                     if p.part == part and p.spacer_count]
        expected = []
        for atom in atoms:
            while additions and additions[0][0] <= atom.start:
                expected.append(additions.pop(0)[1])
            if not _covered(part, atom, patches):
                expected.append(atom.char)
        expected.extend(value for _, value in additions)
        actual = "".join(a.char for a in changed.get(part, []))
        if " ".join("".join(expected).split()) != " ".join(actual.split()):
            return False
    return True


def _actions(patches):
    return [{"format": "docx", "part": p.part,
             "location": {"start_byte": p.start, "end_byte": p.action_end or p.end},
             "technique": p.technique, "visibility": p.visibility, "reason": p.reason,
             "removed_text": p.text, "bytes_removed": p.end - p.start + p.extra_bytes,
             "space_preserved": bool(p.spacer_count), "spacer_width_pt": p.spacer_width_pt}
            for p in patches if p.record]


_LINE_END_HYPHEN = re.compile(r"([-‐‑])[^\S\n]*\n")


def _render_delta_ok(original: str, candidate: str, removed: list[str]) -> bool:
    """Compare rendered character multisets with only bounded line-end hyphenation.

    Whitespace and soft hyphens are ignored. Other added characters must be line-end
    hyphens in the candidate; missing characters must be authorized removals or
    line-end hyphens in the original. Unpainted removed text need not be present.
    """
    from collections import Counter

    def chars(text: str) -> Counter:
        return Counter(c for c in text if not c.isspace() and c != "\u00ad")

    before, after = chars(original), chars(candidate)
    before_line_ends = Counter(_LINE_END_HYPHEN.findall(original))
    after_line_ends = Counter(_LINE_END_HYPHEN.findall(candidate))
    if any(count > after_line_ends[char] for char, count in (after - before).items()):
        return False
    budget = chars("".join(removed)) + before_line_ends
    return all(budget[char] >= count for char, count in (before - after).items())


def _render_pdf(path: Path, root: Path, index: int) -> Path:
    executable = shutil.which("soffice")
    if executable is None:
        raise AdapterError("LibreOffice is unavailable")
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
        raise AdapterError("LibreOffice did not produce a PDF")
    return pdf


def _rendered_chars(pdf):
    """Collect real glyphs in reading order, retaining line and font provenance."""
    chars = []
    for page in pdf:
        for block_index, block in enumerate(page.get_text("rawdict")["blocks"]):
            for line_index, line in enumerate(block.get("lines", [])):
                if tuple(line.get("dir", (1, 0))) != (1, 0):
                    continue
                for span in line["spans"]:
                    for char in span["chars"]:
                        if len(char["c"]) == 1:
                            chars.append((char["c"], (page.number, block_index, line_index),
                                          char["bbox"], span["size"], span["color"], span["font"]))
    return chars


def _median_space(chars) -> float | None:
    spaces = sorted(c[2][2] - c[2][0] for c in chars if c[0] == " " and c[2][2] > c[2][0])
    return spaces[len(spaces) // 2] if spaces else None


def _locate_widths(chars, text: str, reason: str, props: dict[str, Any], cursor: int = 0):
    """Measure a hidden run on the original render: ([(width_pt, spacing_twips)], cursor).

    Complete non-space text is matched in render order at or after ``cursor``;
    repeated text takes the first such occurrence (runs are measured in document order).
    Padding spaces add one space width each; only normal spaces may be omitted at a
    rendered line boundary. Anything else fails closed (None) and the run falls back to
    plain removal, leaving the render gate to decide.
    """
    if not text or any(c.isspace() and c != " " for c in text):
        return None
    color = _rgb(props.get("color"))
    expected_color = (color[0] << 16 | color[1] << 8 | color[2]) if color else None
    eligible = [c for c in chars if (c[3] < 4 if reason == "font smaller than 4 pt"
                                     else expected_color is not None and c[4] == expected_color)]
    core = text.strip(" ")
    if not core:
        size = props.get("size")
        same = [c for c in eligible if size is not None and abs(c[3] - size / 2) < 0.05]
        if len({c[5] for c in same if c[0] == " "}) > 1:
            return None
        space_width = _median_space(same)
        if space_width is None:
            return None
        width = space_width * len(text)
        return [(width, round((width - space_width) * 20))], cursor
    padding = len(text) - len(text.lstrip(" ")), len(text) - len(text.rstrip(" "))
    nonspace = [index for index, char in enumerate(eligible) if not char[0].isspace()]
    rendered = "".join(eligible[index][0] for index in nonspace)
    wanted = "".join(c for c in core if not c.isspace())
    start = rendered.find(wanted)
    if start < 0:
        return None
    if rendered.find(wanted, start + 1) >= 0:
        start = rendered.find(wanted, cursor)
    if start < cursor:
        return None
    selected = eligible[nonspace[start]:nonspace[start + len(wanted) - 1] + 1]
    glyph_index = 0
    for char in core:
        if glyph_index < len(selected) and char == selected[glyph_index][0]:
            glyph_index += 1
        elif (char == " " and 0 < glyph_index < len(selected)
              and selected[glyph_index - 1][1] != selected[glyph_index][1]):
            # LibreOffice can omit a source space at the wrap between these glyphs.
            continue
        else:
            return None
    if glyph_index != len(selected):
        return None
    style = {(round(c[3], 2), c[4], c[5]) for c in selected}
    if len({c[1][0] for c in selected}) != 1 or len(style) != 1:
        return None
    # The PDF glyph box supplies the unexpanded advance of the same font's space.
    space_width = _median_space(selected)
    if space_width is None:
        space_width = _median_space([c for c in eligible
                                     if (round(c[3], 2), c[4], c[5]) in style])
    if space_width is None:
        return None
    groups: list[list] = []
    for char in selected:
        if not groups or groups[-1][0][1] != char[1]:
            groups.append([])
        groups[-1].append(char)
    widths = []
    for number, group in enumerate(groups):
        if any(a[2][0] > b[2][0] + 0.1 for a, b in pairwise(group)):
            return None
        width = group[-1][2][2] - group[0][2][0]
        width += space_width * ((padding[0] if number == 0 else 0) +
                                (padding[1] if number == len(groups) - 1 else 0))
        # A segment narrower than one space (e.g. one glyph wrapped to its own line)
        # keeps a plain space of the same formatting.
        spacing = max(0, round((width - space_width) * 20))
        if not math.isfinite(width) or width <= 0 or spacing > 31680:
            return None
        widths.append((width, spacing))
    return widths, start + len(wanted)


def _spaced_run(raw: bytes, run: Node, text: Node, spacing: int) -> bytes | None:
    """Change only a run's text and its character-spacing property."""
    opening = raw[run.start:run.opening_end]
    if not re.match(rb"<w:r(?:\s|>)", opening):
        return None
    rpr = run.child(W + "rPr")
    if any(child is not rpr and child is not text for child in run.children):
        return None
    text_open = raw[text.start:text.opening_end]
    if text_open == b"<w:t>":
        text_open = b'<w:t xml:space="preserve">'
    elif text_open != b'<w:t xml:space="preserve">':
        return None
    text_bytes = text_open + b" </w:t>"
    spacing_bytes = f'<w:spacing w:val="{spacing}"/>'.encode()
    if rpr is None:
        rpr_bytes = b"<w:rPr>" + spacing_bytes + b"</w:rPr>"
        edits = [(run.opening_end, run.opening_end, rpr_bytes)]
    else:
        head = raw[rpr.start:rpr.opening_end]
        if not re.match(rb"<w:rPr(?:\s|/|>)", head):
            return None
        matches = [child for child in rpr.children if child.tag == W + "spacing"]
        if len(matches) > 1:
            return None
        if matches:
            target = matches[0]
            edits = [(target.start, target.end, spacing_bytes)]
        elif head.endswith(b"/>"):
            edits = [(rpr.start, rpr.end, head[:-2] + b">" + spacing_bytes + b"</w:rPr>")]
        else:
            following = {"w", "kern", "position", "sz", "szCs", "highlight", "u", "effect",
                         "bdr", "shd", "fitText", "vertAlign", "rtl", "cs", "em", "lang",
                         "eastAsianLayout", "specVanish", "oMath"}
            insert = next((child.start for child in rpr.children
                           if child.tag.startswith(W) and child.tag.removeprefix(W) in following),
                          rpr.closing_start)
            edits = [(insert, insert, spacing_bytes)]
    edits.append((text.start, text.end, text_bytes))
    pieces, position = [], run.start
    for start, end, value in sorted(edits):
        pieces.extend((raw[position:start], value))
        position = end
    pieces.append(raw[position:run.end])
    return b"".join(pieces)


def _preserve_spaces(roots, entries, patches, pdf):
    """Replace only unambiguously located hidden text with formatted spaces."""
    chars = _rendered_chars(pdf)
    defaults, styles, default_para = _styles(roots.get("word/styles.xml"))
    by_location = {(p.part, p.start, p.end): p for p in patches}
    replacements = {}
    cursors: dict[str, int] = {}
    for part, root in roots.items():
        if part != "word/document.xml":
            continue  # headers, footers and other parts may be repeated in the render
        for run in (node for node in root.walk() if node.tag == W + "r"):
            texts = [child for child in run.children if child.tag == W + "t"]
            if len(texts) != 1:
                continue
            text = texts[0]
            patch = by_location.get((part, text.start, text.end))
            if patch is None or patch.reason not in {"font smaller than 4 pt",
                                                     "low-contrast foreground against effective background"}:
                continue
            props = _run_properties(run, defaults, styles, default_para)
            located = _locate_widths(chars, patch.text, patch.reason, props,
                                     cursors.get(patch.reason, 0))
            if not located:
                continue
            widths, cursors[patch.reason] = located
            copies = [_spaced_run(entries[part], run, text, spacing) for _, spacing in widths]
            if any(copy is None for copy in copies):
                continue
            replacements[patch] = replace(patch, start=run.start, end=run.end,
                                           replacement=b"".join(copies),
                                           spacer_width_pt=sum(width for width, _ in widths),
                                           spacer_count=len(widths))
    result = sorted((replacements.get(p, p) for p in patches), key=lambda p: (p.part, p.start))
    if any(a.part == b.part and a.end > b.start for a, b in pairwise(result)):
        return patches
    return result


def render_equal(src: Path, candidate: Path, visible: list[str], workdir: Path, *,
                 reference: Path | None = None, hidden: list[str] | None = None,
                 original_pdf: Path | None = None):
    """Compare with a final-view reference containing only the visible removals.

    The original final view supplies affected regions. A second reference with
    precisely the authorized visible edits accounts for natural text reflow;
    every pixel of that reference must equal the delivered candidate.
    """
    executable = shutil.which("soffice")
    if executable is None:
        return False, [], "LibreOffice is unavailable"
    try:
        from doc_sentinel._pymupdf import load as _load_pymupdf
        pymupdf = _load_pymupdf()

        with tempfile.TemporaryDirectory(prefix="doc-sentinel-render-", dir=workdir) as scratch:
            root = Path(scratch)
            pdfs = [original_pdf or _render_pdf(src, root, 0)]
            if reference:
                pdfs.append(_render_pdf(reference, root, 1))
            pdfs.append(_render_pdf(candidate, root, 2))
            with (pymupdf.open(pdfs[0]) as before,
                  pymupdf.open(pdfs[-2]) as expected,
                  pymupdf.open(pdfs[-1]) as after):
                if len(before) != len(expected) or len(expected) != len(after):
                    return False, [], "page count changed"
                page_count = len(before)
                regions = []
                for text in visible:
                    located = [(page.number, rect) for page in before for rect in page.search_for(text)]
                    if not located:
                        return False, regions, "visible injection could not be located in render"
                    regions.extend({"page": number + 1, "rect": list(rect)} for number, rect in located)
                scale = 1.5
                exact = True
                for page_num, (old_page, ref_page, new_page) in enumerate(zip(before, expected, after), 1):
                    images = [page.get_pixmap(matrix=pymupdf.Matrix(scale, scale),
                                              colorspace=pymupdf.csRGB, alpha=False)
                              for page in (old_page, ref_page, new_page)]
                    old, ref, new = images
                    if any((image.width, image.height, image.n) != (old.width, old.height, old.n)
                           for image in images[1:]):
                        return False, regions, "page dimensions changed"
                    if ref.samples != new.samples:
                        exact = False
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
            if exact:
                return True, regions, "all pages compared at 108 DPI against final view", "exact", [1.0] * page_count
            from doc_sentinel.sanitize import _normalized
            from doc_sentinel.sanitize.pdf_to_docx import _similarity

            with pymupdf.open(pdfs[-2]) as expected, pymupdf.open(pdfs[-1]) as after:
                expected_text = "\n".join(_normalized(line) for page in expected
                                          for line in page.get_text().splitlines())
                actual_text = "\n".join(_normalized(line) for page in after
                                        for line in page.get_text().splitlines())
            if not _render_delta_ok(expected_text, actual_text, list(hidden or [])):
                return False, regions, "rendered visible text changed"
            metric = _similarity(pdfs[-2], pdfs[-1])
            scores = [item["similarity"] for item in metric["pages"]]
            if (metric["source_pages"] != metric["output_pages"] or len(scores) != metric["source_pages"] or
                    not scores or any(not math.isfinite(score) or score < 0.97 for score in scores)):
                return False, regions, "rendered visual similarity below 0.97"
            return True, regions, "visible text and page count preserved with reflow", "equivalent_with_reflow", scores
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
    with tempfile.TemporaryDirectory(prefix="doc-sentinel-candidate-", dir=workdir) as scratch:
        root = Path(scratch)
        final_patches = [p for p in patches if p.technique in {
            "tracked_deletion", "tracked_insertion_accepted"}]
        final_view = src
        if final_patches:
            final_view = root / "final-view.docx"
            final_view.write_bytes(write_package(infos, _apply(entries, final_patches),
                                                 {p.part for p in final_patches}, comment, raw, start_dir))
        plain_patches = patches
        original_pdf = None
        if any(p.technique == "hidden-run" and p.reason in {
                "font smaller than 4 pt", "low-contrast foreground against effective background"}
               for p in patches):
            try:
                from doc_sentinel._pymupdf import load as _load_pymupdf

                original_pdf = _render_pdf(final_view, root, 0)
                with _load_pymupdf().open(original_pdf) as pdf:
                    patches = _preserve_spaces(roots, entries, patches, pdf)
            except (OSError, ImportError, ValueError, AdapterError):
                # Plain removal is safe; the existing render gate still decides delivery.
                original_pdf = None
        def attempt(patches):
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
            candidate = root / "candidate.docx"
            candidate.write_bytes(data)
            reference = None
            if visible:
                # Start from the original bytes and apply only final-view changes and
                # explicitly authorized on-page injections, not hidden-content edits.
                reference_patches = [p for p in patches if p in final_patches or p.on_page]
                reference = root / "visible-edits.docx"
                reference.write_bytes(write_package(infos, _apply(entries, reference_patches),
                                                    {p.part for p in reference_patches},
                                                    comment, raw, start_dir))
            comparison = render_equal(final_view, candidate, visible, workdir, reference=reference,
                                      hidden=[p.text for p in patches if p.visibility == "hidden"],
                                      original_pdf=original_pdf)
            equal, regions, reason = comparison[:3]
            details = ({"mode": comparison[3], "similarity": comparison[4]} if len(comparison) == 5 else {})
            checks["render_equal"] = _check("passed" if equal else "failed", reason, regions=regions, **details)
            if not equal:
                return Outcome("refused", actions=actions, checks=checks, reason=reason)
            return Outcome("sanitized", data, actions, checks)

        outcome = attempt(patches)
        if (outcome.status == "refused" and patches != plain_patches and
                outcome.checks.get("render_equal", {}).get("status") == "failed"):
            # Layout spacers are an attempt to keep the page; when the gate rejects
            # them, plain removal (never worse than before spacers) is tried once.
            plain = attempt(plain_patches)
            if plain.status == "sanitized":
                return plain
        return outcome
