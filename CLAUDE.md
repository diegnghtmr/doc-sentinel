# CLAUDE.md — doc-sentinel

Read and follow [AGENTS.md](./AGENTS.md); it is the canonical operating contract.

Run semantic review with `doc-sentinel review-run` as AGENTS.md describes. Do not launch
the `doc-sentinel-reviewer` subagent via the Task/Agent tool for it: a `Read` tool
allowlist does not confine paths, and its reply would pass through this chat.
As the orchestrator, never use Read on inbox
files, reports, review plans, batch files, reviewer answers, or the input document.
If you are the `doc-sentinel-reviewer` subagent, follow `docs/reviewer-prompt.md`:
Read only its assigned batch file, nothing else, and return only its JSON answer.
