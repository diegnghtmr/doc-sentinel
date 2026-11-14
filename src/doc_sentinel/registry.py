"""Which adapters run for which format (PLAN §11) and how they are loaded.

Adapters are imported lazily by id. A missing module or a failed import yields a
placeholder that reports itself unavailable, so the run lists it in ``adapters_skipped``
instead of crashing or pretending the layer ran.
"""

from __future__ import annotations

import importlib
from pathlib import Path

from doc_sentinel.adapters.base import Capability

ADAPTERS: dict[str, str] = {
    "watermarks-remover": "doc_sentinel.adapters.watermarks_remover:WatermarksRemoverAdapter",
    "ooxml-paste-residue": "doc_sentinel.glue.ooxml_paste:OoxmlPasteResidueCleaner",
    "ooxml-fingerprint": "doc_sentinel.glue.ooxml_fingerprint:OoxmlFingerprintCleaner",
    "perlecta": "doc_sentinel.adapters.perlecta:PerlectaAdapter",
    "hidden-text-detector": "doc_sentinel.adapters.hidden_text_detector:HiddenTextDetectorAdapter",
    "subtext-scan": "doc_sentinel.adapters.subtext_scan:SubtextScanAdapter",
    "chk_defaced": "doc_sentinel.adapters.chk_defaced:ChkDefacedAdapter",
    "unicode-smuggling-guard":
        "doc_sentinel.adapters.unicode_smuggling_guard:UnicodeSmugglingGuardAdapter",
    "instruction-phrases": "doc_sentinel.glue.instruction_phrases:InstructionPhrasesAdapter",
    "prompt-guard": "doc_sentinel.adapters.llamafirewall_pg:PromptGuardAdapter",
}

# Channel adapters run after document adapters, including on their revealed text.
CHANNEL_ADAPTERS = frozenset({"unicode-smuggling-guard", "instruction-phrases", "prompt-guard"})

# PLAN §11 and S4/L20. (mandatory, optional) per detected type.
DETECT_MATRIX: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "txt": (("unicode-smuggling-guard", "instruction-phrases"), ("subtext-scan",)),
    "md": (("unicode-smuggling-guard", "instruction-phrases"), ("subtext-scan",)),
    "html": (("subtext-scan", "unicode-smuggling-guard", "instruction-phrases"), ("chk_defaced",)),
    "pdf": (("perlecta", "hidden-text-detector", "chk_defaced", "unicode-smuggling-guard",
             "instruction-phrases"), ("subtext-scan",)),
    "docx": (("perlecta", "hidden-text-detector", "chk_defaced", "unicode-smuggling-guard",
              "instruction-phrases"), ("subtext-scan",)),
    "eml": (("perlecta",), ("unicode-smuggling-guard", "instruction-phrases")),
    "xlsx": ((), ("unicode-smuggling-guard", "instruction-phrases")),
    "pptx": ((), ("unicode-smuggling-guard", "instruction-phrases")),
    "epub": ((), ("unicode-smuggling-guard", "instruction-phrases")),
    "odt": ((), ("unicode-smuggling-guard", "instruction-phrases")),
    "image": (("unicode-smuggling-guard", "instruction-phrases"), ()),
    "svg": (("unicode-smuggling-guard", "instruction-phrases"), ("subtext-scan",)),
}

KNOWN_GAPS: dict[str, str] = {
    "xlsx": "XLSX: structural hidden-content detection is a known gap (PLAN §11); only "
            "extracted text is scanned.",
    "pptx": "PPTX: structural hidden-content detection is a known gap (PLAN §11); only "
            "extracted text is scanned.",
    "epub": "EPUB: detection is partial (PLAN §11); only extracted text is scanned.",
    "odt": "ODT: detection is partial (PLAN §11); only extracted text is scanned.",
    "image": "Images: OCR + channel rules only; pixel watermarks/steganography are not analysed.",
}

CLEAN_MATRIX: dict[str, tuple[str, ...]] = {
    kind: ("watermarks-remover",)
    for kind in ("txt", "md", "html", "pdf", "xlsx", "pptx", "epub", "odt", "image", "svg")
}
CLEAN_MATRIX["docx"] = ("watermarks-remover", "ooxml-fingerprint", "ooxml-paste-residue")


class Unavailable:
    """Stand-in for an adapter whose module cannot be imported."""

    mode = frozenset({"detect", "clean"})

    def __init__(self, adapter_id: str, reason: str) -> None:
        self.id = adapter_id
        self._reason = reason

    def available(self) -> Capability:
        return Capability(False, self._reason)

    def inspect(self, src: Path, ctx):  # pragma: no cover - never called when unavailable
        raise RuntimeError(self._reason)

    def clean(self, src: Path, dest: Path, ctx):  # pragma: no cover
        raise RuntimeError(self._reason)


def load(adapter_id: str):
    target = ADAPTERS.get(adapter_id)
    if target is None:
        return Unavailable(adapter_id, "unknown adapter")
    module_name, _, class_name = target.partition(":")
    try:
        module = importlib.import_module(module_name)
        return getattr(module, class_name)()
    except Exception as exc:  # noqa: BLE001 - any import/init failure means "unavailable"
        return Unavailable(adapter_id, f"adapter not loadable: {type(exc).__name__}: {exc}")


def detect_plan(dtype: str):
    """Return ``(adapters, mandatory_ids)`` for a detected type."""
    mandatory, optional = DETECT_MATRIX.get(dtype, ((), ()))
    return [load(a) for a in (*mandatory, *optional)], set(mandatory)


def clean_chain(dtype: str):
    return [load(a) for a in CLEAN_MATRIX.get(dtype, ())]
