"""Offline ES/EN heuristic rules for instruction phrasing in extracted channels.

Visible text is not necessarily an attack: the same phrases can be discussed as
examples, so only hidden or non-body channels are assigned high severity.
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from doc_sentinel.adapters.base import AdapterError, Capability, RunContext
from doc_sentinel.schema import EXCERPT_MAX, Finding, Location

_SEP = r"[\s_-]+"
_IA = r"(?:i\s*\.?\s*a\.?|inteligencia[\s_-]+artificial|lenguaje)"

# Declaration order is the stable order used for evidence.rules. Family D is
# contextual: it is included only when family A also occurs in that channel.
_RULES = tuple((name, family, re.compile(pattern)) for name, family, pattern in (
    ("ai_addressed_es", "a", (rf"\bsi{_SEP}eres{_SEP}(?:una|un){_SEP}"
        rf"(?:herramienta|modelo|asistente|sistema|programa|chatbot|agente){_SEP}"
        rf"(?:de{_SEP})?{_IA}(?!\w)")),
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
    ("role_es", "c", rf"\beres{_SEP}ahora\b"),
    ("role_en", "c", rf"\byou{_SEP}are{_SEP}now\b"),
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
# Separate prefix and destination searches avoid quadratic backtracking on a
# hostile channel containing many exfiltration prefixes but no destination.
_EXFIL_DESTINATIONS = {
    "exfiltration_en": re.compile(rf"\bto{_SEP}(?:https?\b|www\b)"),
    "exfiltration_es": re.compile(rf"\ba{_SEP}(?:https?\b|www\b)"),
    "markdown_image": re.compile(r"\]\(\s*https?\b"),
}

_HIGH_CHANNELS = frozenset({
    "filename", "metadata", "comment", "annotation", "alt", "hyperlink", "embedded",
    "form_field", "tracked_change", "html_hidden", "image_ocr",
})
_CLASSES = {"filename": "filename_injection", "metadata": "metadata_injection",
            "alt": "link_or_alt_injection", "hyperlink": "link_or_alt_injection"}


def _fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    decomposed = unicodedata.normalize("NFD", normalized)
    stripped = "".join(c for c in decomposed if unicodedata.category(c) not in {"Mn", "Cf"})
    return re.sub(r"\s+", " ", stripped)


def _original_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Map folded offsets back to original text, including stripped format characters."""
    position = 0
    begin = None
    last_space = False
    for i, char in enumerate(text):
        normalized = unicodedata.normalize("NFD", unicodedata.normalize("NFKC", char).casefold())
        for folded_char in normalized:
            if unicodedata.category(folded_char) in {"Mn", "Cf"}:
                continue
            is_space = folded_char.isspace()
            if is_space and last_space:
                continue
            last_space = is_space
            if position == start:
                begin = i
            position += 1
            if position == end:
                return begin if begin is not None else i, i + 1
    # Rare cross-character compatibility compositions can make offsets differ;
    # keep a bounded excerpt instead of failing the whole channel.
    return (begin if begin is not None else 0), len(text)


class InstructionPhrasesAdapter:
    id = "instruction-phrases"
    mode = frozenset({"detect"})

    def available(self) -> Capability:
        return Capability(True)

    def inspect(self, src: Path, ctx: RunContext) -> list[Finding]:
        findings = []
        for channel in ctx.channels:
            text = _fold(channel.text)
            matches = []
            for name, family, pattern in _RULES:
                match = pattern.search(text)
                if match is None:
                    continue
                destination = _EXFIL_DESTINATIONS.get(name)
                if destination is not None:
                    end_match = destination.search(text, match.end())
                    if end_match is None:
                        continue
                    end = end_match.end()
                else:
                    end = match.end()
                matches.append((name, family, match.start(), end))
            if not any(family in {"a", "b", "c", "e"} for _, family, _, _ in matches):
                continue
            has_condition = any(family == "a" for _, family, _, _ in matches)
            matches = [item for item in matches if item[1] != "d" or has_condition]
            _, _, first_start, first_end = min(matches, key=lambda item: item[2])
            start, end = _original_span(channel.text, first_start, first_end)
            left = max(0, start - 120)
            right = min(len(channel.text), end + 120, left + EXCERPT_MAX)
            findings.append(Finding(
                adapter=self.id, adapter_code="INSTRUCTION_PHRASES",
                cls=_CLASSES.get(channel.channel, "prompt_injection"),
                severity="high" if channel.hidden or channel.channel in _HIGH_CHANNELS else "medium",
                confidence="heuristic",
                location=Location(file=src.name, channel=channel.channel, pointer=channel.pointer,
                                  page=channel.page),
                excerpt=channel.text[left:right],
                evidence={"rules": [name for name, _, _, _ in matches], "hidden": channel.hidden,
                          "matched": channel.text[start:end][:160]},
            ))
        return findings

    def clean(self, src: Path, dest: Path, ctx: RunContext):
        raise AdapterError("instruction-phrases is detect-only")
