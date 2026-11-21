# CLAUDE.md — doc-sentinel

Read and follow [AGENTS.md](./AGENTS.md); it is the canonical operating contract.

For Claude Code, use the `doc-sentinel-reviewer` subagent via the Task/Agent tool
only when the isolation and consent requirements in AGENTS.md are met. A `Read`
tool allowlist does not confine paths. As the orchestrator, never use Read on inbox
files, reports, review plans, batch files, reviewer answers, or the input document.
