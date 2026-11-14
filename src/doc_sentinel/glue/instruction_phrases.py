"""Offline ES/EN heuristic rules for instruction phrasing in extracted channels.

Visible text is not necessarily an attack: the same phrases can be discussed as
examples, so only hidden or non-body channels are assigned high severity.
"""
from __future__ import annotations

import bisect
import re
import unicodedata
from pathlib import Path

from doc_sentinel.adapters.base import AdapterError, Capability, RunContext
from doc_sentinel.schema import EXCERPT_MAX, Finding, Location

_SEP = r"[\s_-]+"
_IA = r"(?:i\s*\.?\s*a\.?|inteligencia[\s_-]+artificial|lenguaje)"

_PRODUCT = r"(?:chatgpt|gpt(?:[\s_-]*\d[\w.]*)?|claude|gemini|copilot|bard|llama|dan|grok|mistral)"
_LIMITLESS_ES = rf"sin{_SEP}(?:restricciones|reglas|limites|filtros|censura)"
_LIMITLESS_EN = rf"(?:without|with{_SEP}no){_SEP}(?:rules|restrictions|limits|filters|censorship)"
_ADJ_ES = (rf"(?:(?!(?:en|de|del|para|con|sobre|y|o|que|a)\b)[a-z]+{_SEP}){{0,2}}?")
_ADJ_EN = (rf"(?:(?!(?:in|of|for|on|about|at|with|to|and|or|that|from)\b)[a-z]+{_SEP}){{0,2}}?")
# Generic nouns (sistema, modelo, asistente, agente) only count when qualified as AI or
# as unrestricted; chatbot / bot / IA are AI roles by themselves.
_ROLE_ES = (rf"(?:{_IA}|chatbot|bot)(?!\w)"
            rf"|(?:sistema|modelo|programa|herramienta|asistente|agente){_SEP}"
            rf"(?:(?:de|basado{_SEP}en){_SEP}{_IA}(?!\w)|{_LIMITLESS_ES}\b)"
            rf"|(?:asistente|agente){_SEP}virtual\b")
_ROLE_EN = (rf"(?:ai|chatbot|bot|(?:language|ai){_SEP}model)\b"
            rf"|(?:helpful|unrestricted|uncensored|jailbroken|virtual|evil){_SEP}"
            rf"(?:assistant|agent)\b"
            rf"|(?:assistant|agent){_SEP}{_LIMITLESS_EN}\b")
# A product name is a role only when nothing but punctuation or a connective follows, so
# "Eres ahora Claude Monet" (a painter) is not flagged.
_PRODUCT_ROLE = (rf"(?:(?:microsoft|openai|google|anthropic|meta){_SEP})?{_PRODUCT}(?!\w)"
                 r"(?=\s*(?:$|[^\w\s]|(?:sin|without|que|who|and|y|an?|una?|el|la|the)\b))")

# Declaration order is the stable order used for evidence.rules. Family D is
# contextual: it is included only when family A also occurs in that channel.
_RULES = tuple((name, family, re.compile(pattern)) for name, family, pattern in (
    ("ai_addressed_es", "a", (rf"\bsi{_SEP}eres{_SEP}(?:una|un){_SEP}"
        rf"(?:{_IA}|modelo{_SEP}(?:de{_SEP})?{_IA}|"
        rf"(?:herramienta|asistente|sistema|programa|chatbot|agente){_SEP}"
        rf"(?:de{_SEP})?{_IA})(?!\w)")),
    ("ai_passive_es", "a", (rf"\bsi{_SEP}(?:este{_SEP}documento{_SEP})?"
        rf"(?:esta{_SEP}siendo|es){_SEP}"
        rf"(?:desarrollado|resuelto|procesado|leido|analizado){_SEP}por{_SEP}"
        rf"(?:una{_SEP})?(?:herramienta{_SEP}de{_SEP})?{_IA}(?!\w)")),
    ("ai_addressed_en", "a", (rf"\bif{_SEP}you{_SEP}are{_SEP}an?{_SEP}"
        rf"(?:ai|llm|language{_SEP}model|assistant|chatbot|agent)\b")),
    ("ai_identity_en", "a", rf"\bas{_SEP}an{_SEP}ai(?:{_SEP}language{_SEP}model)?\b"),
    ("override_es", "b", (rf"\bignora{_SEP}(?:todas{_SEP})?(?:las{_SEP})?"
        rf"(?:instrucciones|indicaciones|ordenes){_SEP}"
        rf"(?:anteriores|previas|de{_SEP}arriba|del{_SEP}sistema)\b")),
    ("forget_es", "b", rf"\bolvida{_SEP}(?:todo|las{_SEP}instrucciones)\b"),
    ("override_en", "b", (rf"\bignore{_SEP}(?:all{_SEP})?"
        rf"(?:previous|prior|above|earlier){_SEP}(?:instructions|directions|prompts)\b")),
    ("disregard_en", "b", rf"\bdisregard(?:{_SEP}[a-z]+){{0,6}}{_SEP}instructions\b"),
    ("forget_en", "b", rf"\bforget{_SEP}(?:everything|all){_SEP}(?:above|before)\b"),
    ("system_prompt", "c", rf"\bsystem{_SEP}prompt\b"),
    # Role hijack needs an explicit AI role or a product name; ordinary prose such as
    # "eres ahora mayor de edad", "un modelo a seguir", "un estudiante de IA" or
    # "Claude Monet" must not match. Up to two adjectives may precede the role, but never
    # a preposition (so "un experto en sistemas de IA" stays prose).
    ("role_es", "c", (rf"\b(?:eres{_SEP}ahora|ahora{_SEP}eres){_SEP}(?:"
        rf"(?:un|una|el|la){_SEP}{_ADJ_ES}(?:{_ROLE_ES})|{_PRODUCT_ROLE})")),
    ("role_en", "c", (rf"\byou{_SEP}are{_SEP}now{_SEP}(?:"
        rf"(?:an?|the){_SEP}{_ADJ_EN}(?:{_ROLE_EN})|{_PRODUCT_ROLE})")),
    ("new_instructions_en", "c", rf"\bnew{_SEP}instructions\s*:"),
    ("new_instructions_es", "c", rf"\bnuevas{_SEP}instrucciones\s*:"),
    ("system_token", "c", r"\[system\]"),
    ("chat_token", "c", r"<\|im_start\|>"),
    ("answer_es", "d", (rf"\b(?:inventa|genera|crea){_SEP}(?:tus|los|sus){_SEP}"
        rf"(?:propios{_SEP})?(?:datos|conjuntos|relaciones|valores|numeros)\b")),
    ("omit_es", "d", (rf"\b(?:no{_SEP}tengas{_SEP}en{_SEP}cuenta{_SEP}"
        rf"(?:los{_SEP}datos|el{_SEP}resto|lo{_SEP}anterior)|"
        rf"no{_SEP}(?:menciones|reveles|digas))\b")),
    ("reply_es", "d", rf"\bresponde{_SEP}(?:solo|unicamente|con)\b"),
    ("omit_en", "d", rf"\bdo{_SEP}not{_SEP}(?:mention|reveal|tell)\b"),
    ("exfiltration_en", "e", rf"\bsend{_SEP}"),
    ("exfiltration_es", "e", rf"\b(?:envia|envie){_SEP}"),
    ("markdown_image", "e", r"!\["),
))
_EMAIL = r"[a-z0-9._%+-]{1,64}@[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63}){1,8}"  # bounded: no backtracking blowup
# Complete destinations (scheme, host and path; bounded) so an invisible character anywhere
# inside the URL or e-mail is checked for obfuscation. Markdown links "[text](url)" too.
_URL = r"(?:https?://|www\.)[^\s<>\"'()\[\]]{1,2048}"
_DESTINATION = (rf"(?:[<\"'«(\[]?(?:{_URL}|{_EMAIL})"
                rf"|\[[^\[\]\n]{{0,120}}\]\(\s*{_URL})")
# Separate prefix and destination searches avoid quadratic backtracking on a
# hostile channel containing many exfiltration prefixes but no destination.
_EXFIL_DESTINATIONS = {
    "exfiltration_en": re.compile(rf"\bto{_SEP}{_DESTINATION}"),
    "exfiltration_es": re.compile(rf"\ba{_SEP}{_DESTINATION}"),
    "markdown_image": re.compile(rf"\]\(\s*{_URL}"),
}

_HIGH_CHANNELS = frozenset({
    "filename", "metadata", "comment", "annotation", "alt", "hyperlink", "embedded",
    "form_field", "tracked_change", "html_hidden", "image_ocr",
})
_CLASSES = {"filename": "filename_injection", "metadata": "metadata_injection",
            "alt": "link_or_alt_injection", "hyperlink": "link_or_alt_injection"}


class _Folded:
    """Folded text plus a map back to the original, built in one pass.

    ``origin[i]`` is the original index of folded char ``i`` and ``cf_prefix[j]`` counts the
    invisible format characters (Unicode category Cf) in ``source[:j]``, so mapping a match
    and asking "is there an invisible char inside it?" are O(1) per match.
    """

    __slots__ = ("cf_prefix", "length", "origin", "text")

    def __init__(self, source: str) -> None:
        chars: list[str] = []
        origin: list[int] = []
        cf_prefix = [0]
        last_space = False
        for i, char in enumerate(source):
            cf_prefix.append(cf_prefix[-1] + (unicodedata.category(char) == "Cf"))
            normalized = unicodedata.normalize(
                "NFD", unicodedata.normalize("NFKC", char).casefold())
            for folded_char in normalized:
                if unicodedata.category(folded_char) in {"Mn", "Cf"}:
                    continue
                is_space = folded_char.isspace()
                if is_space and last_space:
                    continue
                last_space = is_space
                chars.append(" " if is_space else folded_char)
                origin.append(i)
        self.text = "".join(chars)
        self.origin = origin
        self.cf_prefix = cf_prefix
        self.length = len(source)

    def span(self, start: int, end: int) -> tuple[int, int]:
        """Original [start, end) covering folded [start, end), stripped chars included."""
        if start >= len(self.origin) or end <= start:
            return 0, self.length
        return self.origin[start], self.origin[end - 1] + 1

    def obfuscated(self, start: int, end: int) -> bool:
        first, last = self.span(start, end)
        return self.cf_prefix[last] - self.cf_prefix[first] > 0


def _fold(text: str) -> str:
    return _Folded(text).text


def _filename_source(filename: str) -> tuple[str, list[int]]:
    """Separate filename words while retaining offsets into the untrusted original."""
    stem = filename[:len(filename) - len(Path(filename).suffix)]
    chars: list[str] = []
    offsets: list[int] = []
    for i, char in enumerate(stem):
        if i and char.isupper() and (stem[i - 1].islower() or (
                stem[i - 1].isupper() and i + 1 < len(stem) and stem[i + 1].islower())):
            chars.append(" ")
            offsets.append(i)
        chars.append(" " if char in "-_." else char)
        offsets.append(i)
    return "".join(chars), offsets


def _rule_spans(name: str, pattern: re.Pattern[str], folded: str) -> list[tuple[int, int]]:
    """Every occurrence of a rule; exfiltration spans run to the next destination.

    Destinations are collected once and paired with bisect, so a hostile channel with many
    prefixes and no destination stays linear.
    """
    destination = _EXFIL_DESTINATIONS.get(name)
    if destination is None:
        return [(m.start(), m.end()) for m in pattern.finditer(folded)]
    targets = [(m.start(), m.end()) for m in destination.finditer(folded)]
    starts = [t[0] for t in targets]
    spans = []
    for match in pattern.finditer(folded):
        k = bisect.bisect_left(starts, match.end())
        if k < len(targets):
            spans.append((match.start(), targets[k][1]))
    return spans


class InstructionPhrasesAdapter:
    id = "instruction-phrases"
    mode = frozenset({"detect"})

    def available(self) -> Capability:
        return Capability(True)

    def inspect(self, src: Path, ctx: RunContext) -> list[Finding]:
        findings = []
        for channel in ctx.channels:
            source, offsets = (_filename_source(channel.text) if channel.channel == "filename"
                               else (channel.text, None))
            folded = _Folded(source)
            text = folded.text
            matches = []
            hidden_in: set[str] = set()
            for name, family, pattern in _RULES:
                spans = _rule_spans(name, pattern, text)
                if not spans:
                    continue
                matches.append((name, family, spans[0][0], spans[0][1]))
                # Invisible format characters inside ANY occurrence of a matched rule,
                # destination included, are deliberate evasion.
                if any(folded.obfuscated(a, b) for a, b in spans):
                    hidden_in.add(name)
            if not any(family in {"a", "b", "c", "e"} for _, family, _, _ in matches):
                continue
            has_condition = any(family == "a" for _, family, _, _ in matches)
            matches = [item for item in matches if item[1] != "d" or has_condition]
            _, _, first_start, first_end = min(matches, key=lambda item: item[2])
            start, end = folded.span(first_start, first_end)
            obfuscated = any(name in hidden_in for name, _, _, _ in matches)
            if offsets is not None:
                start, end = offsets[start], offsets[end - 1] + 1
            left = max(0, start - 120)
            right = min(len(channel.text), end + 120, left + EXCERPT_MAX)
            findings.append(Finding(
                adapter=self.id, adapter_code="INSTRUCTION_PHRASES",
                cls=_CLASSES.get(channel.channel, "prompt_injection"),
                severity=("high" if channel.hidden or obfuscated
                          or channel.channel in _HIGH_CHANNELS else "medium"),
                confidence="heuristic",
                location=Location(file=src.name, channel=channel.channel, pointer=channel.pointer,
                                  page=channel.page),
                excerpt=channel.text[left:right],
                evidence={"rules": [name for name, _, _, _ in matches], "hidden": channel.hidden,
                          "obfuscated": obfuscated, "matched": channel.text[start:end][:160]},
            ))
        return findings

    def clean(self, src: Path, dest: Path, ctx: RunContext):
        raise AdapterError("instruction-phrases is detect-only")
