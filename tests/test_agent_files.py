"""S8/S13: portable, least-privilege agent and reviewer contracts."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import pytest
import yaml

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None


ROOT = Path(__file__).resolve().parents[1]
PROMPT = ROOT / "docs/reviewer-prompt.md"
CLAUDE = ROOT / ".claude/agents/doc-sentinel-reviewer.md"
GEMINI = ROOT / ".gemini/agents/doc-sentinel-reviewer.md"
COPILOT = ROOT / ".github/agents/doc-sentinel-reviewer.agent.md"
OPENCODE = ROOT / ".opencode/agents/doc-sentinel-reviewer.md"
CODEX = ROOT / ".codex/agents/doc-sentinel-reviewer.toml"


def frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    match = re.fullmatch(r"---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    assert match, f"missing YAML frontmatter in {path}"
    config = yaml.safe_load(match.group(1))
    assert isinstance(config, dict)
    return config, match.group(2).strip()


def codex_config() -> dict:
    if tomllib is None:
        pytest.skip("tomli is needed to parse TOML on Python 3.10")
    return tomllib.loads(CODEX.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", [CLAUDE, GEMINI, COPILOT, OPENCODE])
def test_reviewer_markdown_parses_and_embeds_canonical_prompt(path: Path):
    config, body = frontmatter(path)
    assert config.get("description")
    assert body == PROMPT.read_text(encoding="utf-8").strip()


def test_claude_reviewer_is_read_only_by_tool_allowlist():
    config, _ = frontmatter(CLAUDE)
    assert config["name"] == "doc-sentinel-reviewer"
    assert config["tools"] == "Read"
    assert set(config) <= {"name", "description", "tools", "model"}
    assert config.get("model", "inherit") == "inherit"


def test_gemini_reviewer_and_project_context_settings():
    config, _ = frontmatter(GEMINI)
    assert config["name"] == "doc-sentinel-reviewer"
    assert config["kind"] == "local"
    assert config["tools"] == ["read_file"]
    assert 1 <= config["max_turns"] <= 6
    assert set(config) <= {"name", "description", "kind", "tools", "model", "max_turns"}
    settings = json.loads((ROOT / ".gemini/settings.json").read_text(encoding="utf-8"))
    assert "AGENTS.md" in settings["context"]["fileName"]
    assert "GEMINI.md" in settings["context"]["fileName"]


def test_copilot_reviewer_is_read_only_by_tool_allowlist():
    config, _ = frontmatter(COPILOT)
    assert config["name"] == "doc-sentinel-reviewer"
    assert config["tools"] == ["read"]
    assert set(config) == {"name", "description", "tools"}


def test_opencode_denies_tools_except_review_batch_reads():
    config, _ = frontmatter(OPENCODE)
    assert config["mode"] == "subagent"
    assert set(config) == {"description", "mode", "permission"}
    assert config["permission"] == {
        "*": "deny",
        "read": {
            "*": "deny",
            "reports/*/review/batches/*.json": "allow",
            "*/reports/*/review/batches/*.json": "allow",
        },
    }


def test_codex_inherits_sandbox_and_embeds_canonical_prompt():
    config = codex_config()
    assert config["name"] == "doc-sentinel-reviewer"
    assert config["description"]
    assert set(config) == {"name", "description", "developer_instructions"}
    assert config["developer_instructions"].strip() == PROMPT.read_text(encoding="utf-8").strip()
    text = CODEX.read_text(encoding="utf-8").lower()
    assert "inherit" in text and "read-only" in text and "sandbox" in text


def test_reviewer_contract_has_exact_answer_schema_and_bounded_data_rules():
    text = PROMPT.read_text(encoding="utf-8")
    schema_line = next(line for line in text.splitlines() if line.startswith('{"plan_id"'))
    answer = json.loads(schema_line)
    assert set(answer) == {"plan_id", "batch_id", "results"}
    assert len(answer["results"]) == 1
    result = answer["results"][0]
    assert set(result) == {"task_id", "label", "score"}
    assert result["label"] in {"injection", "benign", "unsure"}
    assert 0 <= result["score"] <= 1
    for required in (
        "untrusted", "datamarking", "whitespace", "one batch file", "no other tools",
        "injection", "benign", "unsure", "0 to 1", "no other keys", "no prose",
        "no code fences", "64 KiB", "exactly one result per task_id",
    ):
        assert required in text
    assert text.count("Synthetic example ") == 3


def test_agents_uses_only_the_s13_s14_command_flows():
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    contract = {
        ("detect", frozenset({"--report", "--agent-summary", "--review-dir"})),
        ("detect", frozenset({"--sanitize", "--output-dir", "--report", "--agent-summary"})),
        ("sanitize", frozenset({"--review-dir", "--output-dir", "--report", "--agent-summary"})),
        ("review-merge", frozenset({"--review-dir", "--report", "--agent-summary"})),
        ("clean", frozenset({"--output-dir", "--report", "--agent-summary"})),
        ("update", frozenset({"--json"})),
    }
    commands = [shlex.split(line.strip()) for line in text.splitlines()
                if line.strip().startswith("doc-sentinel ")]
    assert {(command[1], frozenset(part for part in command[2:] if part.startswith("-")))
            for command in commands} == contract


def test_agents_operating_contract_is_safe_and_agent_neutral():
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    for required in (
        "límpialo", "quita marcas", "revisa", "detecta injection", "update pins",
        "inbox/", "reports/<stem>-<UTC timestamp>/", "scripts/doc-sentinel-docker.sh",
        "scripts\\doc-sentinel.cmd", "Windows 11", "review.batches", "review.parallel_ok",
        "doc-sentinel-reviewer", "plan_id", "batch_id", "answers/<batch_id>.json",
        "verbatim", "file-write tool", "single safe path", "review_errors", "monotonic", "Codex",
        "Claude Code", "Gemini", "Copilot", "OpenCode", "Cursor", "Grok", "Antigravity",
        "Windsurf", "Prompt Guard", "Layer B", "--in-place", "chk_defaced",
        "explicit consent", "original", "stderr", "overview", "next_steps", "explanations",
        "Talking to the user",
    ):
        assert required in text
    assert len(text.splitlines()) <= 200


def test_claude_pointer_forbids_opening_user_data():
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "AGENTS.md" in text
    assert "doc-sentinel-reviewer" in text
    assert "Task/Agent" in text
    assert "never use Read" in text
    assert "inbox" in text and "reports" in text


def test_reviewer_contracts_name_the_datamarking_marker():
    from doc_sentinel.review import MARKER
    assert MARKER == "▁"
    files = [PROMPT, CLAUDE, GEMINI, COPILOT, OPENCODE, CODEX]
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "U+2581" in text and MARKER in text, path


def test_never_read_rules_are_scoped_to_the_orchestrator_with_reviewer_exception():
    # Found end-to-end: reviewer subagents loaded AGENTS.md and refused to read their batch.
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    section = agents.split("## Never read the document", 1)[1].split("\n## ", 1)[0]
    assert "orchestrat" in section.lower()
    assert "doc-sentinel-reviewer" in section
    assert "docs/reviewer-prompt.md" in section
    assert "exactly the one batch file" in section
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "doc-sentinel-reviewer" in claude and "docs/reviewer-prompt.md" in claude
    assert "only its assigned batch file" in claude
