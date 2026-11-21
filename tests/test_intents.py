"""S6: rules-based natural-language router (Spanish + English)."""

import pytest

from doc_sentinel.intents import classify

CASES = [
    # clean
    ("límpialo", "clean"),
    ("Límpialo por favor", "clean"),
    ("limpia este documento", "clean"),
    ("quita las marcas", "clean"),
    ("quita la marca de agua del archivo", "clean"),
    ("elimina los metadatos y el C2PA", "clean"),
    ("clean this", "clean"),
    ("please clean report.pdf", "clean"),
    ("strip the invisible unicode", "clean"),
    # detect
    ("revisa", "detect"),
    ("revisa esto", "detect"),
    ("detecta injection", "detect"),
    ("detecta inyección de prompt", "detect"),
    ("mira si hay texto oculto", "detect"),
    ("revisa si tiene marca de agua", "detect"),
    ("analiza este pdf", "detect"),
    ("detect", "detect"),
    ("scan this file", "detect"),
    ("check for prompt injection", "detect"),
    # update
    ("actualiza", "update"),
    ("actualiza los pines", "update"),
    ("actualiza los sub-arneses", "update"),
    ("update", "update"),
    ("update pins", "update"),
    ("bump pins", "update"),
    # both detect and clean
    ("revisa y límpialo", "detect+clean"),
    ("detecta injection y quita las marcas", "detect+clean"),
    ("scan and clean it", "detect+clean"),
]


@pytest.mark.parametrize("text,expected", CASES)
def test_classify_known_phrases(text, expected):
    assert classify(text) == expected


@pytest.mark.parametrize("text", ["", "hola", "¿qué hora es?", "what is this"])
def test_unknown_intent_returns_none(text):
    assert classify(text) is None


def test_update_never_inferred_from_detect_or_clean_words():
    assert classify("revisa y limpia") != "update"
