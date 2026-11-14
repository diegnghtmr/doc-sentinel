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
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import numpy as np
import pikepdf
import pymupdf
from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.text import WD_ALIGN_PARAGRAPH
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
    technique: str = ""
    reason: str = ""
    visibility: str = "hidden"


@dataclass
class _Line:
    chars: list[_Char]
    bbox: tuple[float, float, float, float]


@dataclass
class _Page:
    width: float
    height: float
    lines: list[_Line]
    images: list[dict[str, Any]]
    tables: list[Any]
    links: list[dict[str, Any]]
    no_text: bool


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


def _extract_page(page, off_names, background, images, default_counts):
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
    for block in data["blocks"]:
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
                lines.append(_Line(chars, tuple(line["bbox"])))
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
        tables = list(page.find_tables().tables)
    except (AttributeError, ValueError, RuntimeError) as exc:
        raise ValueError("table detection failed") from exc
    return _Page(page.rect.width, page.rect.height, lines, images, tables,
                 page.get_links(), not bool(lines)), extras


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
        chars = [char for line in pages[number-1].lines for char in line.chars]
        text = "".join(char.text for char in chars)
        start = text.find(span["text"])
        if start < 0 or text.find(span["text"], start + 1) >= 0:
            raise ValueError("injection span is missing or ambiguous")
        for char in chars[start:start + len(span["text"])]:
            if not char.technique:
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
        key = (char.font, char.size, char.flags, char.color, char.background, link)
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


def _paragraph_format(paragraph, line, page):
    left, right = line.bbox[0], page.width-line.bbox[2]
    if abs(left-right) < 10 and left > 50:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    elif right < left * 0.5:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    else:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.space_after = Pt(0)


def _table_chars(page, box):
    rect = _rect(box)
    return [[c for c in line.chars if not c.technique and rect.contains(
        pymupdf.Point((c.bbox[0]+c.bbox[2])/2, (c.bbox[1]+c.bbox[3])/2))]
            for line in page.lines]


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
        section.left_margin, section.right_margin = Pt(max(0, left)), Pt(max(0, right))
        section.top_margin = Pt(max(0, top))
        # Keep reflow headroom even when the original page has a large blank bottom.
        section.bottom_margin = Pt(max(0, min(30, bottom)))
        section.header.is_linked_to_previous = False
        section.footer.is_linked_to_previous = False
        headers, footers = [], []
        for line in page.lines:
            for kind, area, texts in (("header", section.header, headers),
                                      ("footer", section.footer, footers)):
                if (index, id(line), kind) in repeated:
                    p = area.paragraphs[0] if not texts else area.add_paragraph()
                    _write_runs(p, line.chars, page.links)
                    texts.append(_line_text(line))
        table_boxes = [tuple(table.bbox) for table in page.tables]
        items = []
        for line in page.lines:
            if any((index, id(line), kind) in repeated for kind in ("header", "footer")):
                continue
            chars = [c for c in line.chars if not c.technique and not any(
                _rect(box).contains(pymupdf.Point((c.bbox[0]+c.bbox[2])/2,
                                                  (c.bbox[1]+c.bbox[3])/2))
                for box in table_boxes)]
            if chars and "".join(c.text for c in chars).strip():
                items.append((line.bbox[1], line.bbox[0], "line", (line, chars)))
        items.extend((box[1], box[0], "table", table)
                     for box, table in zip(table_boxes, page.tables))
        items.extend((image["bbox"][1], image["bbox"][0], "image", image)
                     for image in page.images)
        body = []
        previous_bottom = top
        for item_top, _, kind, item in sorted(items, key=lambda value: value[:2]):
            gap = max(0, item_top - previous_bottom)
            if kind == "line":
                line, chars = item
                paragraph = doc.add_paragraph()
                _paragraph_format(paragraph, line, page)
                paragraph.paragraph_format.space_before = Pt(gap)
                if paragraph.alignment == WD_ALIGN_PARAGRAPH.LEFT:
                    paragraph.paragraph_format.left_indent = Pt(max(0, line.bbox[0] - left))
                _write_runs(paragraph, chars, page.links)
                previous_bottom = max(previous_bottom, line.bbox[3])
                body.append("".join(c.text for c in chars))
            elif kind == "image":
                if not item.get("data"):
                    raise ValueError("page image cannot be extracted safely")
                box = _rect(item["bbox"])
                paragraph = doc.add_paragraph()
                paragraph.paragraph_format.space_before = Pt(gap)
                paragraph.paragraph_format.space_after = Pt(0)
                paragraph.paragraph_format.left_indent = Pt(max(0, box.x0 - left))
                paragraph.add_run().add_picture(io.BytesIO(item["data"]),
                                                width=Pt(box.width), height=Pt(box.height))
                previous_bottom = max(previous_bottom, box.y1)
            else:
                table = item
                previous_bottom = max(previous_bottom, table.bbox[3])
                if not table.rows or not table.rows[0].cells:
                    raise ValueError("table geometry is incomplete")
                cells = table.rows[0].cells
                columns = []
                for column, box in enumerate(cells):
                    if box is None:
                        raise ValueError("merged PDF table cells are unsupported")
                    # MuPDF can expose the two edges of one stroked line as an empty column.
                    empty = all(not any(_table_chars(page, row.cells[column]))
                                for row in table.rows if row.cells[column] is not None)
                    if _rect(box).width > 3 or not empty:
                        columns.append(column)
                word = doc.add_table(rows=len(table.rows), cols=len(columns))
                for row_index, row in enumerate(table.rows):
                    for col_index, column in enumerate(columns):
                        box = row.cells[column]
                        if box is None:
                            raise ValueError("merged PDF table cells are unsupported")
                        lines = _table_chars(page, box)
                        p = word.cell(row_index, col_index).paragraphs[0]
                        texts = []
                        for chars in lines:
                            if not chars:
                                continue
                            if texts:
                                p.add_run(" ")
                            _write_runs(p, chars, page.links)
                            texts.append("".join(c.text for c in chars))
                        body.append(" ".join(texts))
        page_text = _normalized(" ".join([*headers, *body, *footers]))
        original_text = _normalized(" ".join(_line_text(line) for line in page.lines))
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
                visible_images, default_traces = [], []
                for page in original:
                    if page.rotation:
                        raise ValueError("page rotation cannot be mapped safely")
                    default_traces.append(Counter(_key(chr(code), origin)
                                                  for trace in page.get_texttrace()
                                                  for code, _gid, origin, _bbox in trace["chars"]))
                    images = page.get_image_info(xrefs=True)
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
                                                   default_traces[number])
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
                      "--headless", "--convert-to", "pdf", "--outdir", str(converted_dir),
                      str(candidate)], ctx,
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
