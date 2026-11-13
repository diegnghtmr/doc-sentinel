"""Rules-based natural-language intent router (PLAN §12). Never an LLM verdict.

Returns ``"clean"``, ``"detect"``, ``"update"``, ``"detect+clean"`` or ``None`` (ask the user).
"""

from __future__ import annotations

import re
import unicodedata

_CLEAN = [
    r"\blimpi\w*", r"\bquit\w*", r"\belimin\w*", r"\bborr\w*", r"\bsaniti[sz]\w*",
    r"\bclean\w*", r"\bstrip\w*", r"\bremove\w*", r"\bscrub\w*",
]
_DETECT = [
    r"\brevis\w*", r"\bdetect\w*", r"\banaliz\w*", r"\banalys\w*", r"\bescane\w*",
    r"\baudit\w*", r"\binspec\w*", r"\bscan\w*", r"\bcheck\w*", r"\bmira si\b",
    r"\bverific\w*", r"\binyeccion\w*", r"\binjection\w*",
]
_UPDATE = [r"\bactualiz\w*", r"\bupdate\w*", r"\bbump\w*", r"\bupgrade\w*"]


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _hit(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text) for p in patterns)


def classify(text: str) -> str | None:
    folded = _fold(text or "")
    clean, detect, update = (_hit(_CLEAN, folded), _hit(_DETECT, folded), _hit(_UPDATE, folded))
    if update and not (clean or detect):
        return "update"
    if clean and detect:
        return "detect+clean"
    if clean:
        return "clean"
    if detect:
        return "detect"
    return None
