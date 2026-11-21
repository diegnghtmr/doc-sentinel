"""Every command shown to orchestrators must be present in the public CLI help."""
from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from doc_sentinel import cli

ROOT = Path(__file__).resolve().parents[1]


def test_agent_examples_use_supported_commands_and_flags(capsys):
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```(?:text)?\n(.*?)```", text, flags=re.DOTALL)
    examples = [shlex.split(line.strip()) for block in blocks for line in block.splitlines()
                if line.strip().startswith("doc-sentinel ")]
    assert {args[1] for args in examples} == {"detect", "clean", "sanitize", "review-merge", "update"}
    for args in examples:
        with pytest.raises(SystemExit) as exc:
            cli.main([args[1], "--help"])
        out = capsys.readouterr()
        assert exc.value.code == 0 and out.err == ""
        for flag in (part for part in args[2:] if part.startswith("-")):
            assert re.search(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", out.out), (args, flag)
