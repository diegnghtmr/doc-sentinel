"""Conservative HTML source-span planning; never serialize the document tree.

Only inline display/visibility and the HTML hidden attribute are interpreted.
Stylesheets and scripts are not evaluated. Markup with ambiguous nesting or an
explicit visible override inside a visibility-hidden element is refused.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from itertools import pairwise
from typing import Any

from doc_sentinel.sanitize.text import Edit, invisible_offsets

_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
                   "param", "source", "track", "wbr"})
_REF = re.compile(r"&(?:\#[xX][0-9a-fA-F]+|\#[0-9]+|[a-zA-Z][a-zA-Z0-9]+);?")


@dataclass(frozen=True)
class Atom:
    char: str
    start: int
    end: int
    hidden: bool = False
    comment: bool = False


@dataclass(frozen=True)
class Removal:
    edit: Edit
    removed_text: str


def _style(style: str) -> dict[str, str]:
    # Resolve simple inline declarations, including importance and last-wins.
    # Complex/unknown values are not guessed to be hidden.
    style = re.sub(r"/\*.*?\*/", "", style, flags=re.DOTALL)
    result: dict[str, tuple[bool, str]] = {}
    for part in style.split(";"):
        name, separator, value = part.partition(":")
        name, value = name.strip().lower(), value.strip().lower()
        if not separator or name not in {"display", "visibility"}:
            continue
        important = bool(re.search(r"\s*!important\s*$", value))
        value = re.sub(r"\s*!important\s*$", "", value).strip()
        if name not in result or important or not result[name][0]:
            result[name] = important, value
    return {key: value for key, (_, value) in result.items()}


class _Parser(HTMLParser):
    def __init__(self, source: str):
        super().__init__(convert_charrefs=False)
        self.source = source
        self.lines = [0] + [i + 1 for i, char in enumerate(source) if char == "\n"]
        self.stack: list[dict[str, Any]] = []
        self.atoms: list[Atom] = []
        self.ranges: list[Removal] = []
        self.non_inline = False
        self.has_stylesheet = False

    def _pos(self) -> int:
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def _hidden(self) -> bool:
        return any(node["hidden"] for node in self.stack)

    def _raw_text(self) -> bool:
        return any(node["tag"] in {"script", "style"} for node in self.stack)

    def _new_node(self, tag: str, attrs: list[tuple[str, str | None]]) -> dict[str, Any]:
        attr_map = dict(attrs)
        if len(attr_map) != len(attrs):
            raise ValueError("duplicate HTML attributes are ambiguous")
        props = _style(attr_map.get("style") or "")
        if props.get("visibility") == "visible" and any(
                node["hidden"] == "visibility" for node in self.stack):
            raise ValueError("visible override within visibility-hidden element")
        reason = ("hidden-attribute" if "hidden" in attr_map else
                  "display" if props.get("display") == "none" else
                  "visibility" if props.get("visibility") == "hidden" else "")
        if tag == "style" or (tag == "link" and "stylesheet" in (
                attr_map.get("rel") or "").lower().split()):
            self.has_stylesheet = True
        if self.has_stylesheet or "class" in attr_map:
            self.non_inline = True
        return {"tag": tag, "start": self._pos(), "hidden": reason,
                "atom_start": len(self.atoms), "outer_hidden": self._hidden()}

    def _finish(self, node: dict[str, Any], end: int) -> None:
        if node["hidden"] and not node["outer_hidden"]:
            start = node["start"]
            text = "".join(a.char for a in self.atoms[node["atom_start"]:])
            self.ranges.append(Removal(Edit(start, end, "hidden-element", "hidden",
                "inline visibility or hidden attribute conceals the element"), text))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = self._new_node(tag, attrs)
        if tag in _VOID:
            self._finish(node, self._pos() + len(self.get_starttag_text()))
        else:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _VOID:
            raise ValueError("self-closing non-void HTML tag is ambiguous")
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1]["tag"] != tag:
            raise ValueError("HTML has ambiguous or unbalanced nesting")
        end = self.source.find(">", self._pos())
        if end < 0:
            raise ValueError("unterminated HTML end tag")
        self._finish(self.stack.pop(), end + 1)

    def handle_data(self, data: str) -> None:
        if self._raw_text():
            return
        start = self._pos()
        hidden = self._hidden()
        self.atoms.extend(Atom(char, start + i, start + i + 1, hidden) for i, char in enumerate(data))

    def _reference(self) -> None:
        if self._raw_text():
            return
        start = self._pos()
        match = _REF.match(self.source, start)
        if match is None:
            raise ValueError("invalid HTML character reference")
        raw = match.group()
        self.atoms.extend(Atom(char, start, start + len(raw), self._hidden())
                          for char in html.unescape(raw))

    def handle_entityref(self, name: str) -> None:
        self._reference()

    def handle_charref(self, name: str) -> None:
        self._reference()

    def handle_comment(self, data: str) -> None:
        start = self._pos()
        end = self.source.find("-->", start + 4)
        if end < 0:
            raise ValueError("unterminated HTML comment")
        end += 3
        self.atoms.extend(Atom(char, start, end, self._hidden(), True) for char in data)
        if not self._hidden():
            self.ranges.append(Removal(Edit(start, end, "html-comment", "hidden",
                "HTML comments are not displayed"), data))

    def finish(self) -> None:
        self.feed(self.source)
        self.close()
        if self.stack:
            raise ValueError("HTML has unclosed elements")


def _parse(source: str) -> _Parser:
    parser = _Parser(source)
    parser.finish()
    return parser


def _text_removals(atoms: list[Atom], indexes: set[int], technique: str,
                   visibility: str, reason: str) -> list[Removal]:
    # A character reference can expand to multiple code points: deleting a part
    # would remove unrequested text, so require the complete source reference.
    targets = {(atoms[index].start, atoms[index].end) for index in indexes}
    for index, atom in enumerate(atoms):
        if (atom.start, atom.end) in targets and index not in indexes:
            raise ValueError("a character reference cannot be removed partially")
    selected = [atoms[index] for index in sorted(indexes)]
    groups: list[list[Atom]] = []
    for atom in selected:
        if groups and atom.start <= groups[-1][-1].end:
            groups[-1].append(atom)
        else:
            groups.append([atom])
    return [Removal(Edit(group[0].start, group[-1].end, technique, visibility, reason),
                    "".join(item.char for item in group)) for group in groups]


def plan_html(source: str, spans: list[dict[str, Any]]) -> tuple[list[Removal], _Parser]:
    parsed = _parse(source)
    if parsed.has_stylesheet and any(r.edit.technique == "hidden-element" for r in parsed.ranges):
        raise ValueError("non-inline CSS can override the visibility of a hidden element")
    atoms = [a for a in parsed.atoms if not a.hidden and not a.comment]
    text = "".join(a.char for a in atoms)
    selected: set[int] = set()
    visible: list[Removal] = []
    for span in spans:
        if not isinstance(span, dict):
            raise TypeError("injection span must be an object")
        fragment = span.get("text")
        if (span.get("channel") != "body" or span.get("pointer") not in (None, "body")
                or not isinstance(fragment, str) or not fragment):
            raise ValueError("HTML injection span has an invalid text, channel, or pointer")
        start = text.find(fragment)
        if start < 0 or text.find(fragment, start + 1) >= 0:
            raise ValueError("HTML injection span is missing or ambiguous")
        indexes = set(range(start, start + len(fragment)))
        if selected.intersection(indexes):
            raise ValueError("HTML injection spans overlap")
        selected.update(indexes)
        visible.extend(_text_removals(atoms, indexes, "visible-injection", "visible",
                                     "caller supplied an exact visible injection fragment"))
    invisible = {index for index in invisible_offsets(text, leading_bom=False)
                 if index not in selected and not (atoms[index].start == 0
                                                   and atoms[index].char == "\ufeff")}
    hidden = _text_removals(atoms, invisible, "invisible-unicode", "hidden",
                            "format character creates a view/extraction asymmetry")
    removals = sorted(parsed.ranges + visible + hidden, key=lambda item: item.edit.start)
    if any(a.edit.end > b.edit.start for a, b in pairwise(removals)):
        raise ValueError("HTML edits overlap")
    return removals, parsed


def extract_after(parsed: _Parser, removals: list[Removal]) -> str:
    result = []
    index = 0
    for atom in parsed.atoms:
        while index < len(removals) and removals[index].edit.end <= atom.start:
            index += 1
        edit = removals[index].edit if index < len(removals) else None
        if edit and edit.start < atom.end and edit.end > atom.start:
            if edit.start > atom.start or edit.end < atom.end:
                raise ValueError("partial HTML text token removal")
        else:
            result.append(atom.char)
    return "".join(result)


def actions_for_html(source: str, removals: list[Removal]) -> list[dict[str, Any]]:
    offsets = [0]
    for char in source:
        offsets.append(offsets[-1] + len(char.encode("utf-8")))
    return [{"format": "html", "part": "body",
             "location": {"start_byte": offsets[r.edit.start], "end_byte": offsets[r.edit.end]},
             "technique": r.edit.technique, "visibility": r.edit.visibility,
             "reason": r.edit.reason, "removed_text": r.removed_text,
             "bytes_removed": offsets[r.edit.end] - offsets[r.edit.start]} for r in removals]
