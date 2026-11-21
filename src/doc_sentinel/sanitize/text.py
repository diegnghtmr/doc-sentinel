"""Exact UTF-8 text edits. Offsets always refer to the original decoded text.

Joiners are retained when between letters of the same Arabic or Indic script,
ignoring intervening combining marks, or (ZWJ only) between emoji-range symbols,
ignoring variation selectors and skin-tone modifiers. This is conservative, not
a full Unicode grapheme/shaping implementation. A leading UTF-8 BOM and soft
hyphen are retained: soft hyphen can draw a hyphen at a line break.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from itertools import pairwise
from typing import Any


@dataclass(frozen=True)
class Edit:
    start: int
    end: int
    technique: str
    visibility: str
    reason: str


_SCRIPT_NAMES = (
    "ARABIC", "DEVANAGARI", "BENGALI", "GURMUKHI", "GUJARATI", "ORIYA", "ODIA",
    "TAMIL", "TELUGU", "KANNADA", "MALAYALAM", "SINHALA", "MYANMAR", "KHMER",
    "GRANTHA", "BALINESE", "JAVANESE", "BRAHMI", "SHARADA", "TIRHUTA", "MODI",
    "TAKRI", "NEWA", "SIDDHAM", "KHUDAWADI", "MAHAJANI", "DOGRA", "NANDINAGARI",
)


def _script(char: str) -> str | None:
    if not char or not unicodedata.category(char).startswith("L"):
        return None
    name = unicodedata.name(char, "")
    return next((script for script in _SCRIPT_NAMES if name.startswith(script + " ")), None)


def _neighbor(text: str, position: int, step: int) -> str:
    position += step
    while 0 <= position < len(text):
        char = text[position]
        cp = ord(char)
        if unicodedata.category(char).startswith("M") or 0x1F3FB <= cp <= 0x1F3FF:
            position += step
        else:
            return char
    return ""


def _emoji(char: str) -> bool:
    if not char:
        return False
    code = ord(char)
    return 0x1F000 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF


def invisible_offsets(text: str, *, leading_bom: bool = True) -> list[int]:
    """Identify exactly the removable code points, preserving shaping exceptions."""
    offsets = []
    for index, char in enumerate(text):
        code = ord(char)
        if code == 0xFEFF and leading_bom and index == 0:
            continue
        if not (0xE0000 <= code <= 0xE007F or 0x202A <= code <= 0x202E
                or 0x2066 <= code <= 0x2069 or 0x200B <= code <= 0x200D
                or 0x2060 <= code <= 0x2064 or code == 0xFEFF):
            continue
        if code in (0x200C, 0x200D):
            left, right = _neighbor(text, index, -1), _neighbor(text, index, 1)
            script = _script(left)
            if script is not None and script == _script(right):
                continue
            if code == 0x200D and _emoji(left) and _emoji(right):
                continue
        offsets.append(index)
    return offsets


def plan_text(text: str, spans: list[dict[str, Any]]) -> list[Edit]:
    edits = []
    for span in spans:
        if not isinstance(span, dict):
            raise TypeError("injection span must be an object")
        fragment = span.get("text")
        if (span.get("channel") != "body" or span.get("pointer") not in (None, "body")
                or not isinstance(fragment, str) or not fragment):
            raise ValueError("injection span has an invalid text, channel, or pointer")
        start = text.find(fragment)
        if start < 0 or text.find(fragment, start + 1) >= 0:
            raise ValueError("injection span is missing or ambiguous in the original text")
        edits.append(Edit(start, start + len(fragment), "visible-injection", "visible",
                          "caller supplied an exact visible injection fragment"))
    edits.sort(key=lambda item: item.start)
    if any(a.end > b.start for a, b in pairwise(edits)):
        raise ValueError("injection spans overlap")
    # A format character inside a visible injection is already accounted for by
    # that injection; edits must remain non-overlapping and based on the original.
    visible_edits = list(edits)
    visible_index = 0
    for pos in invisible_offsets(text):
        while visible_index < len(visible_edits) and visible_edits[visible_index].end <= pos:
            visible_index += 1
        if visible_index == len(visible_edits) or visible_edits[visible_index].start > pos:
            edits.append(Edit(pos, pos + 1, "invisible-unicode", "hidden",
                              "format character creates a view/extraction asymmetry"))
    return sorted(edits, key=lambda item: item.start)


def apply_edits(text: str, edits: list[Edit]) -> str:
    pieces = []
    cursor = 0
    for edit in edits:
        if edit.start < cursor or edit.end <= edit.start:
            raise ValueError("overlapping or empty edits")
        pieces.append(text[cursor:edit.start])
        cursor = edit.end
    pieces.append(text[cursor:])
    return "".join(pieces)


def actions_for_text(text: str, edits: list[Edit], kind: str) -> list[dict[str, Any]]:
    offsets = [0]
    for char in text:
        offsets.append(offsets[-1] + len(char.encode("utf-8")))
    return [{"format": kind, "part": "body",
             "location": {"start_byte": offsets[e.start], "end_byte": offsets[e.end]},
             "technique": e.technique, "visibility": e.visibility, "reason": e.reason,
             "removed_text": text[e.start:e.end],
             "bytes_removed": offsets[e.end] - offsets[e.start]} for e in edits]
