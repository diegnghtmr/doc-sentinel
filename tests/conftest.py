"""Shared corpus generation in a disposable system temporary directory."""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

GENERATOR = Path(__file__).parent / "corpus" / "generate.py"


@pytest.fixture(scope="session")
def corpus_directory():
    for module in ("docx", "reportlab", "pikepdf", "pymupdf"):
        pytest.importorskip(module, reason=f"corpus generator needs {module}")
    with tempfile.TemporaryDirectory(prefix="doc-sentinel-corpus-") as scratch:
        directory = Path(scratch) / "fixtures"
        proc = subprocess.run([sys.executable, "-B", str(GENERATOR), "--output", str(directory)],
                              capture_output=True, text=True, timeout=60, check=False)
        assert proc.returncode == 0, proc.stderr
        yield directory
