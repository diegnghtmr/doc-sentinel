"""Bounded, non-reserializing XML primitives for DOCX sanitization."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from xml.parsers import expat

from doc_sentinel.glue.ooxml_paste import _tag_end


@dataclass(frozen=True)
class Atom:
    char: str
    start: int
    end: int


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    start: int
    end: int = 0
    opening_end: int = 0
    closing_start: int = 0
    parent: Node | None = None
    children: list[Node] = field(default_factory=list)
    atoms: list[Atom] = field(default_factory=list)

    def child(self, tag: str) -> Node | None:
        return next((item for item in self.children if item.tag == tag), None)

    def walk(self):
        pending = [self]
        while pending:
            item = pending.pop()
            yield item
            pending.extend(reversed(item.children))

    def ancestor(self, tag: str) -> Node | None:
        node = self.parent
        while node is not None:
            if node.tag == tag:
                return node
            node = node.parent
        return None


def parse_xml(raw: bytes) -> Node:
    """Parse while retaining each element and decoded character's byte offsets."""
    raw.decode("utf-8")
    parser = expat.ParserCreate(namespace_separator="}")
    stack: list[Node] = []
    root: Node | None = None
    inside_cdata = False

    def reject(*_args):
        raise ValueError("DOCTYPE and entities are not supported")

    def start(tag, attrs):
        nonlocal root
        if len(stack) >= 512:
            raise ValueError("XML nesting exceeds limit")
        node = Node(tag, attrs, parser.CurrentByteIndex, parent=stack[-1] if stack else None)
        quote = None
        for pos in range(node.start + 1, len(raw)):
            char = raw[pos]
            if quote is not None:
                if char == quote:
                    quote = None
            elif char in (ord('"'), ord("'")):
                quote = char
            elif char == ord(">"):
                node.opening_end = pos + 1
                break
        if not node.opening_end:
            raise ValueError("unterminated XML opening tag")
        if stack:
            stack[-1].children.append(node)
        else:
            root = node
        stack.append(node)

    def end(_tag):
        node = stack.pop()
        node.end = _tag_end(raw, parser.CurrentByteIndex, node.start)
        node.closing_start = (node.opening_end if raw[node.opening_end - 2:node.opening_end] == b"/>"
                              else parser.CurrentByteIndex)

    def start_cdata():
        nonlocal inside_cdata
        inside_cdata = True

    def end_cdata():
        nonlocal inside_cdata
        inside_cdata = False

    def text(data):
        if not stack:
            return
        start = parser.CurrentByteIndex
        if not inside_cdata and raw[start:start + 1] == b"&":
            finish = raw.find(b";", start)
            if finish < 0:
                raise ValueError("invalid XML reference")
            stack[-1].atoms.extend(Atom(char, start, finish + 1) for char in data)
            return
        for char in data:
            if char == "\n" and raw[start:start + 2] == b"\r\n":
                length = 2
            elif char == "\n" and raw[start:start + 1] == b"\r":
                length = 1
            else:
                length = len(char.encode("utf-8"))
                if raw[start:start + length] != char.encode("utf-8"):
                    raise ValueError("cannot map XML text to original UTF-8 bytes")
            stack[-1].atoms.append(Atom(char, start, start + length))
            start += length

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = text
    parser.StartCdataSectionHandler = start_cdata
    parser.EndCdataSectionHandler = end_cdata
    parser.StartDoctypeDeclHandler = reject
    parser.EntityDeclHandler = reject
    parser.ExternalEntityRefHandler = reject
    try:
        parser.Parse(raw, True)
    except expat.ExpatError as exc:
        raise ValueError("invalid XML") from exc
    if root is None:
        raise ValueError("XML root missing")
    return root


_ATTR = re.compile(rb"\s+([A-Za-z_][\w.:-]*)\s*=\s*([\"'])(.*?)\2", re.DOTALL)
_ENTITIES = {b"amp": "&", b"lt": "<", b"gt": ">", b"apos": "'", b"quot": '"'}


def attribute_atoms(raw: bytes, node: Node, name: str) -> list[Atom]:
    """Map an unqualified attribute's decoded value back to original bytes."""
    opening_end = raw.find(b">", node.start)
    if opening_end < 0:
        raise ValueError("unterminated XML opening tag")
    matches = [m for m in _ATTR.finditer(raw, node.start, opening_end) if m.group(1) == name.encode()]
    if len(matches) != 1:
        raise ValueError("cannot locate exact XML attribute")
    match = matches[0]
    pos, end = match.start(3), match.end(3)
    atoms: list[Atom] = []
    while pos < end:
        if raw[pos:pos + 1] == b"&":
            finish = raw.find(b";", pos + 1, end)
            if finish < 0:
                raise ValueError("invalid XML attribute reference")
            ref = raw[pos + 1:finish]
            if ref.startswith((b"#x", b"#X")):
                char = chr(int(ref[2:], 16))  # noqa: FURB166 - XML reference starts with #x
            elif ref.startswith(b"#"):
                char = chr(int(ref[1:]))
            else:
                char = _ENTITIES.get(ref)
                if char is None:
                    raise ValueError("unsupported XML entity")
            atoms.append(Atom(char, pos, finish + 1))
            pos = finish + 1
        else:
            first = raw[pos]
            length = 1 if first < 0x80 else 2 if first < 0xE0 else 3 if first < 0xF0 else 4
            data = raw[pos:pos + length]
            char = data.decode("utf-8")
            if char == "\r" and raw[pos + length:pos + length + 1] == b"\n":
                length += 1
            if char in "\r\n\t":
                char = " "
            atoms.append(Atom(char, pos, pos + length))
            pos += length
    if "".join(atom.char for atom in atoms) != node.attrs.get(name):
        raise ValueError("attribute byte mapping is ambiguous")
    return atoms
