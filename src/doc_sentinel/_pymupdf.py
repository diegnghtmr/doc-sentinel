"""Single entry point for in-process PyMuPDF use.

PyMuPDF writes notices to stdout by default (e.g. the 'pymupdf_layout' recommendation that
PyMuPDF 1.x prints from find_tables()). doc-sentinel's stdout carries machine-readable JSON
(--json, --agent-summary), so every in-process import goes through load(), which routes
PyMuPDF messages to stderr and disables the layout recommendation once.
"""

from __future__ import annotations

import functools
from types import ModuleType


@functools.lru_cache(maxsize=1)
def load() -> ModuleType:
    import pymupdf

    set_messages = getattr(pymupdf, "set_messages", None)
    if set_messages is not None:
        set_messages(fd=2)
    no_recommend = getattr(pymupdf, "no_recommend_layout", None)
    if no_recommend is not None:
        no_recommend()
    return pymupdf
