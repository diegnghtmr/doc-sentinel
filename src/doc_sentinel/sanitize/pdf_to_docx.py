"""Reconstruct a checked Word document from a PDF without modifying the source.

This is deliberately fail-closed: PDF constructs that cannot be associated with
visible text unambiguously must not result in a delivered document.  The caller
must supply complete (not excerpted) hidden and injection fragments.
"""
from __future__ import annotations

import io
import math
import re
import shutil
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import numpy as np
import pikepdf

from doc_sentinel._pymupdf import load as _load_pymupdf

pymupdf = _load_pymupdf()
from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from doc_sentinel.adapters.base import AdapterError, RunContext, run_tool
from doc_sentinel.extract import extract_channels
from doc_sentinel.glue.instruction_phrases import InstructionPhrasesAdapter
from doc_sentinel.report import atomic_write_bytes, pin_target, same_file
from doc_sentinel.sanitize import SanitizeResult, _check, _normalized


@dataclass
class _Char:
    text: str
    bbox: tuple[float, float, float, float]
    origin: tuple[float, float]
    font: str
    size: float
    flags: int
    color: int
    background: tuple[int, int, int] | None = None
    underline: bool = False
    strike: bool = False
    technique: str = ""
    reason: str = ""
    visibility: str = "hidden"


@dataclass
class _Line:
    chars: list[_Char]
    bbox: tuple[float, float, float, float]
    block: int = 0


@dataclass
class _Page:
    width: float
    height: float
    lines: list[_Line]
    images: list[dict[str, Any]]
    tables: list[Any]
    links: list[dict[str, Any]]
    no_text: bool
    drawings: list[dict[str, Any]]


def _rect(box):
    return pymupdf.Rect(box)


def _key(char, origin):
    return char, round(origin[0], 1), round(origin[1], 1)


def _union(boxes):
    result = pymupdf.Rect(boxes[0])
    for box in boxes[1:]:
        result |= pymupdf.Rect(box)
    return list(result)


def _action(page, chars, technique, reason, visibility="hidden"):
    return {"format": "pdf", "part": f"page {page}",
            "location": _union([c.bbox for c in chars]), "technique": technique,
            "visibility": visibility, "reason": reason,
            "removed_text": "".join(c.text for c in chars)}


def _ocg_off(src: Path):
    with pikepdf.open(src) as pdf:
        oc = pdf.Root.get("/OCProperties")
        if oc is None:
            return set()
        config = oc.get("/D", pikepdf.Dictionary())
        groups = oc.get("/OCGs", [])
        off_ids = {obj.objgen for obj in config.get("/OFF", [])}
        on_ids = {obj.objgen for obj in config.get("/ON", [])}
        base_off = str(config.get("/BaseState", "/ON")) == "/OFF"
        states = [(str(group.get("/Name", "")),
                   group.objgen in off_ids or (base_off and group.objgen not in on_ids))
                  for group in groups]
        names = [name for name, _ in states]
        if len(names) != len(set(names)) or any(not name for name in names):
            raise ValueError("optional-content layer names are ambiguous")
        return {name for name, off in states if off}


def _all_layers(src: Path, target: Path):
    """Enable layers only in a disposable extraction copy, never the input."""
    with pikepdf.open(src) as pdf:
        oc = pdf.Root.get("/OCProperties")
        if oc is not None:
            config = oc.get("/D")
            if config is None:
                raise ValueError("optional-content default configuration is missing")
            config["/BaseState"] = pikepdf.Name.ON
            config["/OFF"] = pikepdf.Array()
            config["/ON"] = oc.get("/OCGs", pikepdf.Array())
        pdf.save(target)


def _without_text(src: Path, target: Path):
    """Make a render-only copy with text-show operators removed, including forms."""
    with pikepdf.open(src) as pdf:
        visited = set()

        def rewrite(obj):
            ident = obj.objgen
            if ident != (0, 0) and ident in visited:
                return
            visited.add(ident)
            operations = pikepdf.parse_content_stream(obj)
            kept = [op for op in operations if str(op.operator) not in {"Tj", "TJ", "'", '"'}]
            data = pikepdf.unparse_content_stream(kept)
            if isinstance(obj, pikepdf.Page):
                obj.Contents = pdf.make_stream(data)
                resources = obj.get("/Resources", pikepdf.Dictionary())
            else:
                obj.write(data)
                resources = obj.get("/Resources", pikepdf.Dictionary())
            for xobj in resources.get("/XObject", {}).values():
                if str(xobj.get("/Subtype", "")) == "/Form":
                    rewrite(xobj)

        for page in pdf.pages:
            rewrite(page)
        pdf.save(target)


def _lab(rgb):
    def linear(value):
        value /= 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(v) for v in rgb)
    x, y, z = ((0.4124*r + 0.3576*g + 0.1805*b) / 0.95047,
               0.2126*r + 0.7152*g + 0.0722*b,
               (0.0193*r + 0.1192*g + 0.9505*b) / 1.08883)
    x, y, z = (v ** (1/3) if v > 216/24389 else (24389/27*v + 16)/116
               for v in (x, y, z))
    return 116*y - 16, 500*(x-y), 200*(y-z)


def _background_colors(char, pix):
    if pix is None:
        return None
    x0, y0, x1, y1 = char.bbox
    scale = 2
    left, top = max(0, int(x0 * scale)), max(0, int(y0 * scale))
    right, bottom = min(pix.width, max(left + 1, math.ceil(x1 * scale))), min(
        pix.height, max(top + 1, math.ceil(y1 * scale)))
    if left >= right or top >= bottom:
        return None
    samples = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    return np.unique(samples[top:bottom, left:right, :3].reshape(-1, 3), axis=0)


def _same_background(char, colors):
    if char.size >= 2 or colors is None:
        return False
    fg = ((char.color >> 16) & 255, (char.color >> 8) & 255, char.color & 255)
    return all(math.dist(_lab(fg), _lab(tuple(int(v) for v in color))) < 2 for color in colors)


def _uniform_background(colors):
    if colors is None or not len(colors):
        return None
    spread = np.max(colors, axis=0).astype(int) - np.min(colors, axis=0).astype(int)
    if np.any(spread > 3):
        return None
    return tuple(int(v) for v in colors[0])


def _extract_page(page, off_names, background, images, default_counts, drawings):
    traces = page.get_texttrace()
    trace_index = {}
    all_counts = Counter()
    for trace in traces:
        for code, _gid, origin, bbox in trace["chars"]:
            key = _key(chr(code), origin)
            all_counts[key] += 1
            trace_index.setdefault(key, []).append((trace, bbox))
    if any(count > all_counts[key] for key, count in default_counts.items()):
        raise ValueError("optional-content configuration hides default-visible text")
    if any(0 < default_counts[key] < count for key, count in all_counts.items()):
        raise ValueError("optional-content visibility is ambiguous for overlapping glyphs")
    off_keys = {key for key in all_counts if not default_counts[key]}
    matched = set()
    lines = []
    data = page.get_text("rawdict", sort=True)
    for block_index, block in enumerate(data["blocks"]):
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            chars = []
            for span in line["spans"]:
                for item in span["chars"]:
                    char = _Char(item["c"], tuple(item["bbox"]), tuple(item["origin"]),
                                 span["font"], span["size"], span["flags"], span["color"])
                    key = _key(char.text, char.origin)
                    refs = trace_index.get(key, [])
                    if not refs:
                        # MuPDF synthesizes spaces across gaps; they have no painted glyph.
                        if char.text != " ":
                            raise ValueError("text extraction could not be matched to rendering trace")
                        chars.append(char)
                        continue
                    matched.add(key)
                    visible = [(t, b) for t, b in refs if key not in off_keys
                               and t.get("type") != 3 and t.get("opacity", 1) > 0
                               and t.get("layer", "") not in off_names]
                    colors = _background_colors(char, background)
                    char.background = _uniform_background(colors)
                    if not visible:
                        if key in off_keys or all(t.get("layer", "") in off_names for t, _ in refs):
                            char.technique, char.reason = "optional-content-off", "layer is off"
                        elif all(t.get("opacity", 1) <= 0 for t, _ in refs):
                            char.technique, char.reason = "zero-opacity", "text opacity is zero"
                        else:
                            char.technique, char.reason = "render-mode-3", "text is not painted"
                    elif not _rect(char.bbox).intersects(page.rect):
                        char.technique, char.reason = "outside-cropbox", "text is outside the visible page"
                    elif _same_background(char, colors):
                        char.technique = "tiny-background-match"
                        char.reason = "text is smaller than 2 pt and matches the rendered background"
                    chars.append(char)
            if chars:
                lines.append(_Line(chars, tuple(line["bbox"]), block_index))
    # Text wholly outside the CropBox is omitted by rawdict but can still occur in trace.
    extras = []
    for trace in traces:
        group = []
        for code, _gid, origin, bbox in trace["chars"]:
            key = _key(chr(code), origin)
            if key in matched:
                continue
            char = _Char(chr(code), tuple(bbox), tuple(origin), trace.get("font", ""),
                         trace.get("size", 0), 0, 0)
            if not _rect(bbox).intersects(page.rect):
                char.technique, char.reason = "outside-cropbox", "text is outside the visible page"
            elif key in off_keys or trace.get("layer", "") in off_names:
                char.technique, char.reason = "optional-content-off", "layer is off"
            elif trace.get("type") == 3 or trace.get("opacity", 1) <= 0:
                char.technique, char.reason = "render-mode-3", "text is not painted"
            else:
                raise ValueError("rendered text was not captured in reading order")
            group.append(char)
        if group:
            extras.append(group)
    try:
        try:
            tables = list(page.find_tables(use_layout=False, paths=drawings).tables)
        except TypeError:
            try:
                tables = list(page.find_tables(paths=drawings).tables)
            except TypeError:
                if off_names:
                    raise ValueError("table detection cannot safely isolate OFF-layer graphics")
                tables = list(page.find_tables().tables)
    except (AttributeError, ValueError, RuntimeError) as exc:
        raise ValueError("table detection failed") from exc
    _decorate(lines, drawings)
    return _Page(page.rect.width, page.rect.height, lines, images, tables,
                 page.get_links(), not bool(lines), drawings), extras


def _decorate(lines, drawings):
    """Recognize only thin horizontal marks whose ends coincide with a text run."""
    marks = []
    for drawing in drawings:
        width = drawing.get("width") or 0
        for item in drawing.get("items", []):
            if item[0] == "l" and width <= 1.5:
                start, end = item[1:3]
                if abs(start.y - end.y) <= 0.5:
                    marks.append((min(start.x, end.x), max(start.x, end.x), (start.y + end.y) / 2))
            elif item[0] == "re" and drawing.get("fill") is not None:
                box = _rect(item[1])
                if box.height <= 1.5:
                    marks.append((box.x0, box.x1, (box.y0 + box.y1) / 2))
    for line in lines:
        groups = []
        for char in line.chars:
            if char.technique:
                continue
            style = (char.font, char.size, char.flags, char.color)
            if not groups or groups[-1][0] != style:
                groups.append((style, []))
            groups[-1][1].append(char)
        for _, chars in groups:
            if not chars:
                continue
            left, right = min(c.bbox[0] for c in chars), max(c.bbox[2] for c in chars)
            baseline, size = chars[0].origin[1], chars[0].size
            for x0, x1, y in marks:
                if abs(x0 - left) > 2 or abs(x1 - right) > 2:
                    continue
                delta = y - baseline
                if 0.03 * size <= delta <= 0.3 * size:
                    for char in chars:
                        char.underline = True
                elif -0.45 * size <= delta <= -0.2 * size:
                    for char in chars:
                        char.strike = True


def _normalized_positions(text):
    chars, positions = [], []
    for index, char in enumerate(text):
        if char.isspace():
            if chars and chars[-1] != " ":
                chars.append(" ")
                positions.append(index)
        else:
            chars.append(char)
            positions.append(index)
    if chars and chars[-1] == " ":
        chars.pop()
        positions.pop()
    return "".join(chars), positions


def _validate_spans(pages, hidden_regions, injection_spans):
    for region in hidden_regions:
        if not isinstance(region, dict) or not isinstance(region.get("text"), str):
            raise TypeError("hidden region must contain complete text")
        number, box = region.get("page"), region.get("bbox")
        if not isinstance(number, int) or not 1 <= number <= len(pages) or not box:
            raise ValueError("hidden region lacks a valid page or bounding box")
        selected = [char for line in pages[number-1].lines for char in line.chars
                    if _rect(char.bbox).intersects(_rect(box))]
        text, positions = _normalized_positions("".join(char.text for char in selected))
        wanted = _normalized(region["text"])
        start = text.find(wanted)
        if not wanted or start < 0 or text.find(wanted, start + 1) >= 0:
            raise ValueError("hidden region text could not be located unambiguously")
        begin, end = positions[start], positions[start + len(wanted) - 1] + 1
        for char in selected[begin:end]:
            if not char.technique:
                char.technique = str(region.get("technique") or "hidden-region")
                char.reason = "matched a detected hidden region"

    link_actions = []
    for span in injection_spans:
        if not isinstance(span, dict) or not isinstance(span.get("text"), str) or not span["text"]:
            raise ValueError("injection span must contain complete text")
        number = span.get("page")
        if not isinstance(number, int) or not 1 <= number <= len(pages):
            raise ValueError("injection span lacks a valid page")
        if span.get("channel") == "hyperlink":
            links = [link for link in pages[number-1].links if link.get("uri") == span["text"]]
            if len(links) != 1:
                raise ValueError("hyperlink injection is missing or ambiguous")
            link = links[0]
            pages[number-1].links.remove(link)
            link_actions.append({"format": "pdf", "part": f"page {number}",
                                 "location": list(link["from"]), "technique": "hyperlink-injection",
                                 "visibility": "hidden", "reason": "caller supplied an injection URL",
                                 "removed_text": span["text"]})
            continue
        if span.get("channel") != "body":
            raise ValueError("non-body injection spans cannot be reconstructed safely")
        # The planner joins wrapped PDF lines with spaces; retain a map back to
        # real characters so synthetic separators are never edited as glyphs.
        chars = []
        for line in pages[number-1].lines:
            if chars:
                chars.append(None)
            chars.extend(line.chars)
        text = "".join(char.text if char is not None else " " for char in chars)
        normalized, positions = _normalized_positions(text)
        wanted = _normalized(span["text"])
        start = normalized.find(wanted) if wanted else -1
        if start < 0 or normalized.find(wanted, start + 1) >= 0:
            raise ValueError("injection span is missing or ambiguous")
        begin, end = positions[start], positions[start + len(wanted) - 1] + 1
        for char in chars[begin:end]:
            if char is not None and not char.technique:
                char.technique, char.reason, char.visibility = (
                    "visible-injection", "caller supplied an exact injection fragment", "visible")
    return link_actions


def _collect_actions(pages, extra):
    actions = []
    for number, page in enumerate(pages, 1):
        for group in [line.chars for line in page.lines] + extra[number-1]:
            pending = []
            for char in [*group, None]:
                if pending and (char is None or not char.technique or
                                (char.technique, char.reason, char.visibility) != (
                                    pending[-1].technique, pending[-1].reason, pending[-1].visibility)):
                    first = pending[0]
                    actions.append(_action(number, pending, first.technique, first.reason,
                                           first.visibility))
                    pending = []
                if char is not None and char.technique:
                    pending.append(char)
    return actions


def _font_name(name):
    name = re.sub(r"^[A-Z]{6}\+", "", name)
    lower = name.lower()
    if lower.startswith(("helvetica", "arial")):
        return "Arial"
    if lower.startswith(("times", "timesnewroman")):
        return "Times New Roman"
    if lower.startswith("courier"):
        return "Courier New"
    return re.sub(r"[-,](?:bold|italic|oblique|regular|roman).*", "", name, flags=re.IGNORECASE)


def _write_runs(paragraph, chars, links):
    current = []
    previous = None

    def emit(group, link):
        if not group:
            return
        sample = group[0]
        text = "".join(c.text for c in group)
        run = paragraph.add_run(text)
        run.font.name = _font_name(sample.font)
        run.font.size = Pt(max(0.5, sample.size))
        run.bold = bool(sample.flags & 16 or "bold" in sample.font.lower())
        run.italic = bool(sample.flags & 2 or any(s in sample.font.lower() for s in ("italic", "oblique")))
        run.underline = sample.underline
        run.font.strike = sample.strike
        run.font.color.rgb = RGBColor((sample.color >> 16) & 255, (sample.color >> 8) & 255,
                                      sample.color & 255)
        if sample.background is not None and sample.background != (255, 255, 255):
            shade = OxmlElement("w:shd")
            shade.set(qn("w:val"), "clear")
            shade.set(qn("w:fill"), "".join(f"{value:02X}" for value in sample.background))
            run._r.get_or_add_rPr().append(shade)
        if link:
            rid = paragraph.part.relate_to(link, "http://schemas.openxmlformats.org/officeDocument/"
                                           "2006/relationships/hyperlink", is_external=True)
            hyperlink = OxmlElement("w:hyperlink")
            hyperlink.set(qn("r:id"), rid)
            run._r.getparent().remove(run._r)
            hyperlink.append(run._r)
            paragraph._p.append(hyperlink)

    for char in chars:
        if char.technique:
            continue
        link = next((item.get("uri") for item in links if item.get("uri") and
                     _rect(item["from"]).intersects(_rect(char.bbox))), None)
        if char.color == 0xFFFFFF and char.background is None and char.text.strip():
            raise ValueError("cannot preserve light text on a varying background")
        key = (char.font, char.size, char.flags, char.color, char.background,
               char.underline, char.strike, link)
        if previous is not None and previous != key:
            emit(current, previous[-1])
            current = []
        current.append(char)
        previous = key
    if current:
        emit(current, previous[-1])


def _line_text(line):
    return "".join(char.text for char in line.chars if not char.technique)


def _repeated(pages):
    if len(pages) < 2:
        return set()
    occurrences = {}
    for index, page in enumerate(pages):
        for line in page.lines:
            text = _normalized(_line_text(line))
            if not text:
                continue
            location = "header" if line.bbox[1] < page.height * 0.12 else (
                "footer" if line.bbox[3] > page.height * 0.88 else "")
            if location:
                key = (location, text, round(line.bbox[0] / 5), round(line.bbox[1] / 5))
                occurrences.setdefault(key, set()).add(index)
    accepted = {key for key, seen in occurrences.items() if len(seen) >= math.ceil(2*len(pages)/3)}
    return {(index, id(line), key[0]) for index, page in enumerate(pages) for line in page.lines
            for key in accepted if _normalized(_line_text(line)) == key[1]
            and round(line.bbox[0] / 5) == key[2] and round(line.bbox[1] / 5) == key[3]}


def _line_size(line):
    sizes = [char.size for char in line.chars if not char.technique and char.text.strip()]
    return max(sizes, default=0)


def _baseline(line):
    return next((char.origin[1] for char in line.chars if not char.technique and char.text.strip()),
                line.bbox[3])


def _columns(lines, page):
    """Find clear, vertically overlapping text columns; otherwise return no columns."""
    if page.tables or page.images or len(lines) < 4:
        return []
    groups = []
    for line in sorted(lines, key=lambda item: item.bbox[0]):
        found = next((group for group in groups if line.bbox[0] <= group[1] + 10 and
                      line.bbox[2] >= group[0] - 10), None)
        if found is None:
            groups.append([line.bbox[0], line.bbox[2], [line]])
        else:
            found[0], found[1] = min(found[0], line.bbox[0]), max(found[1], line.bbox[2])
            found[2].append(line)
    groups.sort(key=lambda group: group[0])
    if not 2 <= len(groups) <= 3 or any(len(group[2]) < 2 for group in groups):
        return []
    if any(b[0] - a[1] < 24 for a, b in pairwise(groups)):
        return []
    tops = [min(line.bbox[1] for line in group[2]) for group in groups]
    bottoms = [max(line.bbox[3] for line in group[2]) for group in groups]
    if max(tops) >= min(bottoms):
        return []
    ids = {id(line): index for index, group in enumerate(groups) for line in group[2]}
    order = [ids[id(line)] for line in lines]
    if order != sorted(order):
        return []
    return groups


def _mergeable(group, line):
    previous = group[-1][0]
    size = _line_size(previous)
    if not size or previous.block != line.block or abs(size - _line_size(line)) > 0.3:
        return False
    distance = _baseline(line) - _baseline(previous)
    if not 0.8 * size <= distance <= 1.8 * size:
        return False
    reference = group[1][0].bbox[0] if len(group) > 1 else previous.bbox[0]
    tolerance = 2 if len(group) > 1 else max(2, size * 1.8)
    return abs(line.bbox[0] - reference) <= tolerance


def _paragraph_format(paragraph, group, left, right, page_width=None, exact_single=False):
    lines = [item[0] for item in group]
    first = lines[0]
    if len(lines) > 1 and max(line.bbox[2] for line in lines[:-1]) >= right - 12 and all(
            abs(line.bbox[0] - lines[0].bbox[0]) <= 2 and line.bbox[2] >= right - 12
            for line in lines[:-1]):
        alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    else:
        if page_width is not None and len(lines) == 1:
            lspace, rspace = first.bbox[0], page_width - first.bbox[2]
        else:
            lspace, rspace = first.bbox[0] - left, right - first.bbox[2]
        if abs(lspace-rspace) < 10 and lspace > 10:
            alignment = WD_ALIGN_PARAGRAPH.CENTER
        elif rspace < lspace * 0.5:
            alignment = WD_ALIGN_PARAGRAPH.RIGHT
        else:
            alignment = WD_ALIGN_PARAGRAPH.LEFT
    paragraph.alignment = alignment
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.space_after = Pt(0)
    if alignment == WD_ALIGN_PARAGRAPH.LEFT:
        base = lines[1].bbox[0] if len(lines) > 1 else first.bbox[0]
        paragraph.paragraph_format.left_indent = Pt(max(0, base - left))
        paragraph.paragraph_format.right_indent = Pt(max(0, right - max(line.bbox[2] for line in lines)))
        if len(lines) > 1:
            paragraph.paragraph_format.first_line_indent = Pt(first.bbox[0] - base)
    if len(lines) > 1:
        distances = sorted(_baseline(b) - _baseline(a) for a, b in pairwise(lines))
        paragraph.paragraph_format.line_spacing = Pt(distances[len(distances) // 2])
    elif exact_single:
        paragraph.paragraph_format.line_spacing = Pt(first.bbox[3] - first.bbox[1])


def _anchor_picture(paragraph, image, box, behind=False):
    run = paragraph.add_run()
    run.add_picture(io.BytesIO(image), width=Pt(box.width), height=Pt(box.height))
    inline = run._r.xpath("./w:drawing/wp:inline")[0]
    anchor = OxmlElement("wp:anchor")
    for name, value in {"distT": "0", "distB": "0", "distL": "0", "distR": "0",
                        "simplePos": "0", "relativeHeight": "0", "behindDoc": str(int(behind)),
                        "locked": "0", "layoutInCell": "1", "allowOverlap": "1"}.items():
        anchor.set(name, value)
    simple = OxmlElement("wp:simplePos")
    simple.set("x", "0")
    simple.set("y", "0")
    anchor.append(simple)
    # LibreOffice shifts an anchored image upward by one twip; compensate for
    # that export rounding without changing the displayed image dimensions.
    for tag, position in (("positionH", box.x0), ("positionV", box.y0 + 0.05)):
        element = OxmlElement("wp:" + tag)
        element.set("relativeFrom", "page")
        offset = OxmlElement("wp:posOffset")
        offset.text = str(int(Pt(position)))
        element.append(offset)
        anchor.append(element)
    for child in list(inline):
        inline.remove(child)
        anchor.append(child)
        if child.tag == qn("wp:extent"):
            effect = OxmlElement("wp:effectExtent")
            for side in ("l", "t", "r", "b"):
                effect.set(side, "0")
            anchor.append(effect)
            anchor.append(OxmlElement("wp:wrapNone"))
    inline.getparent().replace(inline, anchor)


def _table_chars(page, box):
    rect = _rect(box)
    return [[c for c in line.chars if not c.technique and rect.contains(
        pymupdf.Point((c.bbox[0]+c.bbox[2])/2, (c.bbox[1]+c.bbox[3])/2))]
            for line in page.lines]


def _boundaries(values):
    result = []
    for value in sorted(values):
        if not result or value - result[-1] > 2:
            result.append(value)
    return result


def _drawing_rgb(color):
    if not color:
        return None
    if len(color) == 1:
        color = color * 3
    if len(color) != 3:
        return None
    return "".join(f"{max(0, min(255, int(value * 255 + 0.5001))):02X}" for value in color)


def _cell_appearance(cell, box, drawings):
    """Keep a cell's solid fill and explicitly drawn, unambiguous borders."""
    x0, y0, x1, y1 = box
    fill = None
    edges = {}
    for drawing in drawings:
        color = _drawing_rgb(drawing.get("fill"))
        if color and drawing.get("fill_opacity", 1) > 0:
            for item in drawing.get("items", []):
                if item[0] == "re":
                    area = _rect(item[1])
                    if (area.x0 <= x0 + 1 and area.y0 <= y0 + 1 and
                            area.x1 >= x1 - 1 and area.y1 >= y1 - 1):
                        fill = color
        color = _drawing_rgb(drawing.get("color"))
        if color is None or drawing.get("stroke_opacity", 1) <= 0:
            continue
        width = max(2, min(96, round((drawing.get("width") or 0.25) * 8)))
        segments = []
        for item in drawing.get("items", []):
            if item[0] == "l":
                segments.append((item[1].x, item[1].y, item[2].x, item[2].y))
            elif item[0] == "re":
                rect = _rect(item[1])
                segments.extend(((rect.x0, rect.y0, rect.x1, rect.y0),
                                 (rect.x0, rect.y1, rect.x1, rect.y1),
                                 (rect.x0, rect.y0, rect.x0, rect.y1),
                                 (rect.x1, rect.y0, rect.x1, rect.y1)))
        for ax, ay, bx, by in segments:
            if abs(ay - by) <= 1 and min(ax, bx) <= x0 + 1 and max(ax, bx) >= x1 - 1:
                if abs(ay-y0) <= 1:
                    edges["top"] = color, width
                if abs(ay-y1) <= 1:
                    edges["bottom"] = color, width
            if abs(ax - bx) <= 1 and min(ay, by) <= y0 + 1 and max(ay, by) >= y1 - 1:
                if abs(ax-x0) <= 1:
                    edges["left"] = color, width
                if abs(ax-x1) <= 1:
                    edges["right"] = color, width
    props = cell._tc.get_or_add_tcPr()
    if fill:
        shade = OxmlElement("w:shd")
        shade.set(qn("w:val"), "clear")
        shade.set(qn("w:fill"), fill)
        props.append(shade)
    borders = OxmlElement("w:tcBorders")
    for side in ("top", "left", "bottom", "right"):
        border = OxmlElement("w:" + side)
        if side in edges:
            color, width = edges[side]
            border.set(qn("w:val"), "single")
            border.set(qn("w:color"), color)
            border.set(qn("w:sz"), str(width))
        else:
            border.set(qn("w:val"), "nil")
        borders.append(border)
    props.append(borders)


def _insert_table(doc, page, table):
    boxes = [box for row in table.rows for box in row.cells if box is not None]
    if not boxes:
        raise ValueError("table geometry is incomplete")
    xs = _boundaries([value for box in boxes for value in (box[0], box[2])])
    ys = _boundaries([value for box in boxes for value in (box[1], box[3])])
    if len(xs) < 2 or len(ys) < 2:
        raise ValueError("table grid cannot be resolved")
    word = doc.add_table(rows=len(ys) - 1, cols=len(xs) - 1)
    word.autofit = False
    word.style = "Table Grid"
    for col, (a, b) in enumerate(pairwise(xs)):
        word.columns[col].width = Pt(b - a)
        for row in word.rows:
            row.cells[col].width = Pt(b - a)
    body = []
    for box in sorted(boxes, key=lambda item: (item[1], item[0])):
        x0, y0, x1, y1 = box
        c0 = min(range(len(xs)), key=lambda i: abs(xs[i] - x0))
        c1 = min(range(len(xs)), key=lambda i: abs(xs[i] - x1))
        r0 = min(range(len(ys)), key=lambda i: abs(ys[i] - y0))
        r1 = min(range(len(ys)), key=lambda i: abs(ys[i] - y1))
        if c1 <= c0 or r1 <= r0:
            raise ValueError("table cell span is ambiguous")
        cell = word.cell(r0, c0)
        if c1 - c0 > 1 or r1 - r0 > 1:
            cell = cell.merge(word.cell(r1 - 1, c1 - 1))
        _cell_appearance(cell, box, page.drawings)
        p = cell.paragraphs[0]
        texts = []
        for chars in _table_chars(page, box):
            if not chars:
                continue
            if texts:
                p.add_run(" ")
            _write_runs(p, chars, page.links)
            texts.append("".join(c.text for c in chars))
        body.append(" ".join(texts))
    return body


def _render_document(pages, dest):
    doc = Document()
    core = doc.core_properties
    core.author = core.last_modified_by = core.comments = ""
    repeated = _repeated(pages)
    expected = []
    for index, page in enumerate(pages):
        section = doc.sections[0] if index == 0 else doc.add_section(WD_SECTION.NEW_PAGE)
        section.page_width, section.page_height = Pt(page.width), Pt(page.height)
        active = [line for line in page.lines if _line_text(line).strip() and not any(
            (index, id(line), kind) in repeated for kind in ("header", "footer"))]
        boxes = ([line.bbox for line in active] + [image["bbox"] for image in page.images] +
                 [table.bbox for table in page.tables])
        left = min([box[0] for box in boxes] or [30])
        right = min([page.width-box[2] for box in boxes] or [30])
        top = min([box[1] for box in boxes] or [30])
        bottom = min([page.height-box[3] for box in boxes] or [30])
        columns = _columns(active, page)
        tight_margins = (len(active) == 1 and not page.images and not page.tables and
                         not any(c.technique for c in active[0].chars) and not any(
                             entry[0] == index for entry in repeated))
        # Tight margins are safe for a single line with exact line spacing. More
        # complex pages need headroom to prevent Word from adding a page or wrap.
        section.left_margin = Pt(max(0, left))
        section.right_margin = Pt(max(0, right if tight_margins else right - 4))
        first = min(active, key=lambda line: line.bbox[1]) if active else None
        if not tight_margins and first is not None and abs(first.bbox[1] - top) < 1:
            # Word's baseline in an ordinary paragraph is approximately 15/16 em
            # below the top margin; source bbox ascenders vary by PDF font.
            top_margin = _baseline(first) - _line_size(first) * 0.9375
        else:
            top_margin = top
        section.top_margin = Pt(max(0, top_margin))
        section.bottom_margin = Pt(max(0, bottom if tight_margins else min(30, bottom)))
        section.header_distance = Pt(0)
        section.footer_distance = Pt(0)
        section.header.is_linked_to_previous = False
        section.footer.is_linked_to_previous = False
        if columns:
            cols = section._sectPr.find(qn("w:cols"))
            cols.set(qn("w:num"), str(len(columns)))
            gap = min(b[0] - a[1] for a, b in pairwise(columns))
            cols.set(qn("w:space"), str(int(gap * 20)))
        headers, footers = [], []
        for line in page.lines:
            for kind, area, texts in (("header", section.header, headers),
                                      ("footer", section.footer, footers)):
                if (index, id(line), kind) in repeated:
                    p = area.paragraphs[0] if not texts else area.add_paragraph()
                    _write_runs(p, line.chars, page.links)
                    texts.append(_line_text(line))
                    if kind == "header":
                        section.header_distance = Pt(max(0, line.bbox[1]))
                    else:
                        section.footer_distance = Pt(max(0, page.height-line.bbox[3]))
        table_boxes = [tuple(table.bbox) for table in page.tables]
        items = []
        for line in active:
            chars = [c for c in line.chars if not c.technique and not any(
                _rect(box).contains(pymupdf.Point((c.bbox[0]+c.bbox[2])/2,
                                                  (c.bbox[1]+c.bbox[3])/2))
                for box in table_boxes)]
            if chars and "".join(c.text for c in chars).strip():
                col = next((n for n, group in enumerate(columns) if line in group[2]), 0)
                items.append((col, line.bbox[1], line.bbox[0], "line", (line, chars)))
        items.extend((0, box[1], box[0], "table", table)
                     for box, table in zip(table_boxes, page.tables))
        floating = []
        for image in page.images:
            box = _rect(image["bbox"])
            full_page = not active and len(page.images) == 1 and box.x0 <= 1 and box.y0 <= 1 and (
                box.x1 >= page.width - 1 and box.y1 >= page.height - 1)
            nearby = [line for line in active if box.y0 < line.bbox[3] and box.y1 > line.bbox[1]]
            beside = bool(nearby) and all(box.x1 <= line.bbox[0] or box.x0 >= line.bbox[2]
                                          for line in nearby)
            if beside and (any(box.intersects(_rect(t)) for t in table_boxes) or any(
                    other is not image and box.intersects(_rect(other["bbox"]))
                    for other in page.images)):
                beside = False
            if full_page or beside:
                floating.append((image, box, full_page))
            else:
                items.append((0, box.y0, box.x0, "image", image))
        items.sort(key=lambda value: value[:3])
        body, groups = [], []
        for col, y, x, kind, item in items:
            if kind == "line" and groups and groups[-1][3] == "line" and groups[-1][0] == col and (
                    _mergeable(groups[-1][4], item[0])):
                groups[-1][4].append(item)
            else:
                groups.append([col, y, x, kind, [item] if kind == "line" else item])
        previous_bottom, previous_col, first_paragraph = top, 0, None
        joined_lines = set()
        for col, item_top, _, kind, item in groups:
            if col != previous_col:
                if first_paragraph is None:
                    raise ValueError("column break has no preceding paragraph")
                doc.paragraphs[-1].add_run().add_break(WD_BREAK.COLUMN)
                previous_bottom = top
                previous_col = col
            gap = max(0, item_top - previous_bottom)
            if kind == "line":
                group = item
                paragraph = doc.add_paragraph()
                first_paragraph = first_paragraph or paragraph
                col_left = columns[col][0] if columns else left
                col_right = columns[col][1] if columns else page.width - right
                _paragraph_format(paragraph, group, col_left, col_right,
                                  page.width if not columns else None,
                                  len(active) > 1 or tight_margins)
                if tight_margins:
                    current = paragraph.paragraph_format.right_indent
                    paragraph.paragraph_format.right_indent = Pt((current.pt if current else 0) - 4)
                paragraph.paragraph_format.space_before = Pt(gap)
                for position, (line, chars) in enumerate(group):
                    text = "".join(c.text for c in chars)
                    joined = False
                    if position:
                        previous_line, previous_chars = group[position - 1]
                        previous_text = "".join(c.text for c in previous_chars)
                        joined = bool(previous_text.endswith(("-", "‐", "‑")) and text and
                                      text[0].isalpha())
                        paragraph.add_run("" if joined else " ").add_break()
                        if joined:
                            joined_lines.add((id(previous_line), id(line)))
                    _write_runs(paragraph, chars, page.links)
                    if joined:
                        body[-1] += text
                    else:
                        body.append(text)
                previous_bottom = max(previous_bottom, group[-1][0].bbox[3])
            elif kind == "image":
                if not item.get("data"):
                    raise ValueError("page image cannot be extracted safely")
                box = _rect(item["bbox"])
                paragraph = doc.add_paragraph()
                first_paragraph = first_paragraph or paragraph
                paragraph.paragraph_format.space_before = Pt(gap)
                paragraph.paragraph_format.space_after = Pt(0)
                paragraph.paragraph_format.left_indent = Pt(max(0, box.x0 - left))
                paragraph.add_run().add_picture(io.BytesIO(item["data"]),
                                                width=Pt(box.width), height=Pt(box.height))
                previous_bottom = max(previous_bottom, box.y1)
            else:
                previous_bottom = max(previous_bottom, item.bbox[3])
                body.extend(_insert_table(doc, page, item))
        if floating:
            if first_paragraph is None:
                first_paragraph = doc.add_paragraph()
                first_paragraph.paragraph_format.space_before = Pt(0)
                first_paragraph.paragraph_format.space_after = Pt(0)
                first_paragraph.paragraph_format.line_spacing = Pt(1)
            for image, box, full_page in floating:
                if not image.get("data"):
                    raise ValueError("page image cannot be extracted safely")
                _anchor_picture(first_paragraph, image["data"], box, behind=full_page)
        page_text = _normalized(" ".join([*headers, *body, *footers]))
        original_parts, previous_line = [], None
        for line in page.lines:
            text = _line_text(line)
            if not text.strip():
                continue
            if previous_line is not None and (id(previous_line), id(line)) in joined_lines:
                original_parts[-1] += text
            else:
                original_parts.append(text)
            previous_line = line
        original_text = _normalized(" ".join(original_parts))
        if page_text != original_text:
            raise ValueError("PDF reading order or visible text changed during reconstruction")
        expected.append(page_text)
    doc.save(dest)
    return expected


def _actual_text(doc):
    word = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    parts = [[]]
    for child in doc.element.body:
        if child.tag == word + "sectPr":
            continue
        if child.tag == word + "tbl":
            for row in child.findall(word + "tr"):
                for cell in row.findall(word + "tc"):
                    parts[-1].append("".join(n.text or "" for n in cell.iter(word + "t")))
        else:
            parts[-1].append("".join(n.text or "" for n in child.iter(word + "t")))
            if child.find(word + "pPr/" + word + "sectPr") is not None:
                parts.append([])
    if len(parts) != len(doc.sections):
        raise ValueError("Word sections did not round-trip")
    result = []
    for section, body in zip(doc.sections, parts):
        header = [p.text for p in section.header.paragraphs]
        footer = [p.text for p in section.footer.paragraphs]
        result.append(_normalized(" ".join([*header, *body, *footer])))
    return result


def _redetect(candidate, actions, workdir):
    channels = extract_channels(candidate, "docx")
    findings = InstructionPhrasesAdapter().inspect(candidate, RunContext(workdir=workdir,
                                                                         channels=channels))
    if findings:
        return False
    with zipfile.ZipFile(candidate) as archive:
        if "docProps/custom.xml" in archive.namelist() or any(
                re.fullmatch(r"word/comments\d*\.xml", name) for name in archive.namelist()):
            return False
        texts = []
        for name in archive.namelist():
            if name.startswith(("word/", "docProps/")) and name.endswith(
                    (".xml", ".rels")):
                root = ET.fromstring(archive.read(name))
                # Text may be split between differently formatted Word runs.  Scan the
                # concatenated stream as well as individual XML text nodes and links.
                texts.append("".join(root.itertext()))
                texts.extend(root.itertext())
                texts.extend(value for node in root.iter() for key, value in node.attrib.items()
                             if key.endswith("Target"))
        text = _normalized(" ".join(texts))
        return not any(_normalized(action["removed_text"]) in text for action in actions
                       if _normalized(action["removed_text"]))


def _similarity(src, converted):
    with pymupdf.open(src) as before, pymupdf.open(converted) as after:
        pages = []
        for number, (a, b) in enumerate(zip(before, after), 1):
            arrays = []
            for page in (a, b):
                pix = page.get_pixmap(matrix=pymupdf.Matrix(0.5, 0.5),
                                      colorspace=pymupdf.csGRAY, alpha=False)
                raw = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width)
                y = np.linspace(0, pix.height-1, 128).astype(int)
                x = np.linspace(0, pix.width-1, 128).astype(int)
                arrays.append(raw[np.ix_(y, x)].astype(float))
            x, y = arrays
            mx, my = x.mean(), y.mean()
            covariance = ((x-mx)*(y-my)).mean()
            value = ((2*mx*my + 6.5025)*(2*covariance + 58.5225) /
                     ((mx*mx + my*my + 6.5025)*(x.var() + y.var() + 58.5225)))
            pages.append({"page": number, "similarity": float(value)})
        return {"status": "reported", "detail": "grayscale structural similarity, not a gate",
                "pages": pages, "source_pages": len(before), "output_pages": len(after)}


def pdf_to_docx(src: Path, dest: Path, *, workdir: Path,
                hidden_regions: list[dict] | None = None,
                injection_spans: list[dict] | None = None,
                has_glyph_divergence: bool = False) -> SanitizeResult:
    """Deliver a verified DOCX or refuse, keeping the source and target unchanged."""
    src, dest, workdir = Path(src), Path(dest), Path(workdir)
    actions, checks = [], {}
    try:
        if same_file(src, dest):
            return SanitizeResult("refused", refusal_reason="output path aliases the input")
        if dest.exists() or dest.is_symlink():
            return SanitizeResult("refused", refusal_reason="output path already exists")
        if not workdir.is_dir() or not dest.parent.is_dir():
            return SanitizeResult("refused", refusal_reason="work or output directory is missing")
        if has_glyph_divergence:
            return SanitizeResult("refused", refusal_reason="glyph/ToUnicode divergence would carry replaced characters")
        executable = shutil.which("soffice")
        if not executable:
            return SanitizeResult("refused", refusal_reason="LibreOffice is unavailable")
        target = pin_target(dest, protect=(src,))
        off_names = _ocg_off(src)
        with tempfile.TemporaryDirectory(prefix="doc-sentinel-pdf-word-", dir=workdir) as tmp:
            root = Path(tmp)
            textless, layers = root / "textless.pdf", root / "layers.pdf"
            _without_text(src, textless)
            _all_layers(src, layers)
            with pymupdf.open(src) as original:
                if not original.is_pdf or original.needs_pass or not len(original):
                    raise ValueError("PDF is empty, encrypted or invalid")
                visible_images, default_traces, visible_drawings = [], [], []
                for page in original:
                    if page.rotation:
                        raise ValueError("page rotation cannot be mapped safely")
                    default_traces.append(Counter(_key(chr(code), origin)
                                                  for trace in page.get_texttrace()
                                                  for code, _gid, origin, _bbox in trace["chars"]))
                    images = page.get_image_info(xrefs=True)
                    visible_drawings.append(page.get_drawings())
                    for image in images:
                        extracted = original.extract_image(image.get("xref", 0))
                        image["data"] = extracted.get("image") if extracted else None
                    visible_images.append(images)
            with pymupdf.open(layers) as pdf, pymupdf.open(textless) as background:
                pages, extra = [], []
                for number in range(len(pdf)):
                    page = pdf[number]
                    pix = background[number].get_pixmap(matrix=pymupdf.Matrix(2, 2),
                                                        colorspace=pymupdf.csRGB, alpha=False)
                    item, outliers = _extract_page(page, off_names, pix, visible_images[number],
                                                   default_traces[number], visible_drawings[number])
                    pages.append(item)
                    extra.append(outliers)
                link_actions = _validate_spans(pages, hidden_regions or [], injection_spans or [])
                actions = _collect_actions(pages, extra) + link_actions
                candidate_dir = root / "candidate"
                candidate_dir.mkdir()
                # Filename is an extractable channel and must match the delivered name.
                candidate = candidate_dir / dest.name
                expected = _render_document(pages, candidate)
            reopened = Document(candidate)
            equal = _actual_text(reopened) == expected
            checks["text_equal"] = _check("passed" if equal else "failed",
                                          "normalized document-order text compared per page")
            clean = _redetect(candidate, actions, root)
            checks["redetect_clean"] = _check("passed" if clean else "failed",
                                               "instruction phrases and removed fragments checked")
            if not equal or not clean:
                return SanitizeResult("refused", actions, checks, "DOCX text self-check failed")
            ctx = RunContext(workdir=root, timeout_s=120)
            converted_dir = root / "converted"
            converted_dir.mkdir()
            run_tool([executable, f"-env:UserInstallation={(root / 'profile').as_uri()}",
                      "--headless", "--convert-to",
                      'pdf:writer_pdf_Export:{"UseLosslessCompression":{"type":"boolean","value":"true"}}',
                      "--outdir", str(converted_dir), str(candidate)], ctx,
                     extra_env={"XDG_CONFIG_HOME": str(root / "config"),
                                "XDG_CACHE_HOME": str(root / "cache")}, ok_codes=(0,))
            converted = converted_dir / f"{candidate.stem}.pdf"
            if not converted.is_file():
                raise ValueError("LibreOffice did not produce a PDF")
            checks["opens"] = _check("passed", "python-docx and LibreOffice opened the output")
            checks["visual_similarity"] = _similarity(src, converted)
            checks["text_layer"] = {"status": "reported", "detail": "no text layer" if any(
                page.no_text for page in pages) else "text layer present"}
            atomic_write_bytes(target, candidate.read_bytes())
        return SanitizeResult("sanitized", actions, checks)
    except (OSError, TypeError, ValueError, RuntimeError, ImportError, zipfile.BadZipFile,
            ET.ParseError, AdapterError, pikepdf.PdfError) as exc:
        return SanitizeResult("refused", actions, checks, f"cannot sanitize safely: {exc}")
