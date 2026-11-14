# doc-sentinel

Meta-harness that **cleans** deterministic AI-tool traces from documents and **detects**
prompt injection (hidden text, Unicode smuggling, visual-vs-extracted divergence), by
orchestrating third-party sub-harnesses pinned to exact `main` SHAs. The design and its
rationale are in [`PLAN.md`](./PLAN.md); agent rules are in [`AGENTS.md`](./AGENTS.md).

```text
doc-sentinel clean  <file> [-o PATH] [--force-output] [--json] [--report PATH]
doc-sentinel detect <file> [--sanitize [-o PATH] [--force-output]] [--json|--agent-summary] [--report PATH] [--review-dir DIR] [--allow-network] [--fail-on high|medium] [--prompt-guard]
doc-sentinel sanitize <file> --review-dir DIR [-o PATH] [--force-output] [--report PATH] [--agent-summary]
doc-sentinel review-merge --review-dir DIR [--report PATH] [--agent-summary]
doc-sentinel update [--dry-run] [--only ID] [--json]
doc-sentinel version
doc-sentinel intent "<natural-language request>"   # -> clean | detect | update | detect+clean
```

| Exit | `clean` | `detect` | `update` |
|------|---------|----------|----------|
| 0 | cleaned / already clean | `clean`, or only findings below `--fail-on` | all pins unchanged or updated |
| 1 | residual C2PA / AI metadata after cleaning | `findings` at or above `--fail-on` | a pin was rolled back |
| 2 | tool error, unsupported format, unsafe path | error (a mandatory layer did not run) | fetch error / operational failure |

Guarantees: the input is never modified (`clean` writes `<stem>.cleaned<suffix>` or `-o`,
refuses the input path and existing files without `--force-output`); reports are only written
outside the document; the verdict comes from a deterministic aggregator, never from an agent;
`detect` runs offline: scrubbed environment, `HF_HUB_OFFLINE=1`, and every sub-harness process in
an empty network namespace (`unshare -rn`). If that namespace cannot be created, offline steps
**fail closed** (adapter error, never `clean`) unless isolation is declared external with
`DOC_SENTINEL_NO_NETNS=1` (as the containers do, since they run with `--network=none`).
That declaration is verified: it is honored only when the process sees no network interface
other than loopback; otherwise `unshare -rn` is used, or the step fails closed.
If channel extraction fails (e.g. an archive part over the size limit), the verdict is `error`.

## Optional sanitized copy

When explicitly requested, use `detect FILE --sanitize --report REPORT --agent-summary`
to scan the original and create `<stem>.sanitized<suffix>` beside it, or specify `-o`.
For PDF input the output is a reconstructed `<stem>.sanitized.docx`; `-o` must end in `.docx`.
After semantic review and `review-merge`, use `sanitize FILE --review-dir DIR --report
REPORT --agent-summary`; the original input is mounted again and its SHA-256 must match
the review plan. The detect verdict remains the original document's verdict. A refused
or unsupported copy exits 2; `sanitized` and `nothing_to_do` retain the detect exit
(0 or 1), and `nothing_to_do` creates no copy. Existing outputs require `--force-output`;
an existing target is only atomically replaced when all checks pass, and is preserved
on refusal.

The separate report records each action and its location, technique, visibility, reason and
removed text. The agent summary includes only status, output path, a safe refusal reason,
and counts by technique and visibility, never removed text. Visible instructions are removed
by sentence; hidden content is removed by run or element. DOCX, HTML, TXT and MD are supported
within conservative exact-span limits. For a PDF finding without a bounding box, every match of
complete native text on the applicable page(s) must be independently verified hidden. Recognized
aggregate findings require every span meeting their criterion to be verified and excluded, with the
sample present among those exclusions. Other partial or ambiguous findings are refused.
Glyph/ToUnicode divergence or unlocatable PDF hidden text is refused rather than guessed.
Checks include opening the copy, extraction deltas, re-detection,
and DOCX page-by-page rendering (rendering is not fixed-page for TXT/MD/HTML). Tracked changes
use the final view: deletions are removed, insertions are accepted, and rendering is compared
against a final-view copy of the original.

PDF reconstruction uses visible content, not an editable copy of the original PDF:

| Content | Reconstruction |
| --- | --- |
| Text and layout | Preserves text and estimates reading order, paragraphs, font family/size/style/color, alignment, margins and page breaks; layout may differ. |
| Tables, links, images | Rebuilds detected tables and visible links; retains images with approximate placement and size. Repeated top/bottom lines can become headers/footers. |
| Scanned pages | Keeps page images; **no OCR text is added**. |
| Hidden and malicious text | Excludes identified hidden content and visible injection sentences; ambiguous regions are refused. |

The report includes per-page visual similarity between the original PDF and the rendered DOCX;
its minimum and mean appear in the safe summary. This metric is informative, not a pass/fail gate.

## Sub-harnesses

| id | Role | License | Invocation |
|----|------|---------|------------|
| watermarks-remover | clean: Layer A Unicode, metadata, embedded-media C2PA | MIT | `inspect_file.py` / `clean_file.py` scripts only (never its HTTP `/clean` or Layer B) |
| perlecta-mcp | detect: hidden text in PDF/DOCX/EML | Apache-2.0 | `perlecta analyze --json` (confined) |
| hidden-text-detector | detect: pixel contrast hidden text | MIT | `scripts/scan.py --json` |
| subtext-scan | detect: HTML hidden text + English instruction phrasing | Apache-2.0 | `node bin/subtext-scan.mjs scan --json` |
| chk_defaced-rs | detect: ToUnicode / glyph remapping, invisible text | AGPL-3.0-only | separate process only (`bin/chk_defaced`) |
| unicode-smuggling-guard | detect: tags, bidi, zero-width in extracted channels | MIT | library in a child process |
| instruction-phrases (own glue) | detect: deterministic Spanish/English instruction rules on channels | MIT | in-process, offline, no model |
| PurpleLlama / LlamaFirewall | detect: Prompt Guard 2 classifier on channels, explicit opt-in only | MIT (LlamaFirewall) | child process, offline, never AlignmentCheck |

Other own deterministic glue (added only where the user's samples proved a gap): channel extractor
(filename, metadata, comments, alt, links, tracked changes, embedded names, form fields, OCR),
OOXML HTML-paste residue cleaner, OOXML library-fingerprint cleaner.

## Setup (Linux/macOS)

```bash
git submodule update --init --recursive
uv venv -p 3.12 .venv
uv pip install -p .venv -e '.[dev,detect]' -e 'third_party/perlecta-mcp[rendu]' \
  -e third_party/unicode-smuggling-guard
(cd third_party/subtext-scan && npm ci --ignore-scripts)          # Node >= 22.13
CARGO_TARGET_DIR=.cache/cargo cargo build --release \
  --manifest-path third_party/chk_defaced-rs/Cargo.toml && mkdir -p bin \
  && cp .cache/cargo/release/chk_defaced bin/                      # rustc >= 1.88
.venv/bin/doc-sentinel version
```

Optional system tools: `tesseract` (+`spa`), `qpdf`, `exiftool`, `c2patool` (inspection only).
Without exiftool/c2patool, `clean` warns that metadata and C2PA verification are less reliable.

### Prompt Guard 2 (gated model)

The model is **never used by default**. An explicit `detect --prompt-guard` opt-in adds the
local classifier as an optional channel adapter. It requires the `promptguard` Python extras
and an already-cached `meta-llama/Llama-Prompt-Guard-2-86M` snapshot; the gated model's Llama
license applies. Detection never downloads the model, even with `--allow-network`.

```bash
doc-sentinel detect example.pdf --json --prompt-guard
```

If unavailable, the adapter appears in `adapters_skipped` without changing the verdict. The
default ES/EN rules work without it; both rules and classifier can miss attacks.

## Containers

One image, `doc-sentinel:local` (`containers/Dockerfile`, about 1.5 GB), holds the complete
harness: every pinned sub-harness, chk_defaced as a separate binary, Tesseract (spa/eng), qpdf,
exiftool, c2patool and headless LibreOffice (needed by the DOCX/PDF sanitize render checks).
It never contains your documents (`.dockerignore`) and runs as uid 10001 with no network,
a read-only root, `/tmp` as tmpfs, all capabilities dropped and `no-new-privileges`.

The launchers build the image on first use and pass the same arguments as the CLI:

```bash
scripts/doc-sentinel-docker.sh detect inbox/file.pdf --agent-summary --report reports/file.json
scripts/doc-sentinel-docker.sh clean  inbox/file.docx -o reports/file.cleaned.docx
scripts/doc-sentinel-docker.sh detect inbox/file.docx --sanitize --report reports/file.json
```

The input's folder is mounted read-only; outputs, reports and review directories are mounted
read-write separately. `update` and `--allow-network` are refused inside the container: run
`update` from a git checkout with the host CLI.

### Windows 11 (Docker Desktop)

`detect` cannot run natively on Windows (it needs Linux network namespaces and Perlecta's POSIX
confinement), so on Windows doc-sentinel always runs in the container.

1. Install Docker Desktop (`winget install -e --id Docker.DockerDesktop`) and keep it in
   **Linux containers** mode (the default).
2. Clone with submodules: `git clone --recursive <repo>` (or
   `git submodule update --init --recursive` in an existing clone).
3. From the repository folder, in PowerShell or `cmd`:

   ```powershell
   .\scripts\doc-sentinel.cmd detect "C:\Users\you\Documents\exam.docx" --agent-summary --report "C:\Users\you\Documents\exam.report.json"
   .\scripts\doc-sentinel.cmd detect "C:\Users\you\Documents\exam.docx" --sanitize
   .\scripts\doc-sentinel.cmd clean  "C:\Users\you\Documents\exam.docx"
   ```

The launcher detects Windows 11 (build 22000 or later; older builds get a warning), starts
Docker Desktop and waits for it when it is not running, refuses Windows-containers mode, builds
the image the first time (several minutes, needs internet once), validates on the host that the
output and report are not the input, and maps your folders to the container mounts. Output and
report folders must already exist. Paths with spaces and accents are supported. Exit codes and
JSON output are passed through unchanged, so coding agents (Claude Code, Codex, Copilot, ...)
follow the same `AGENTS.md` flow on Windows.

Status: the PowerShell launcher is tested on Linux PowerShell with a simulated `docker`; the
Windows-specific branches (build detection, Docker Desktop start, drive-letter paths) are
simulated and still need a run on a real Windows 11 machine.

`containers/compose.yaml` offers the same hardening for manual use:

```bash
docker compose -f containers/compose.yaml run --rm detect detect /work/in/file.pdf --json
docker compose -f containers/compose.yaml run --rm clean  clean  /work/in/file.pdf -o /work/out/file.cleaned.pdf
```

## Known gaps (not covered, by design or by the pinned tools)

- Statistical text watermarks (SynthID-Text, textGrain) and pixel watermarks are **not** removed:
  that would require rewriting content, which is prohibited (PLAN principle 2).
- XLSX/PPTX/EPUB/ODT detect: extracted text only, no structural hidden-content analysis.
- subtext-scan phrasing rules are English; instruction-phrases adds deterministic ES/EN rules,
  not general language understanding. Other languages, paraphrases, and extraction failures can
  be missed; visible discussion can also produce medium-severity false positives.
- OCR failures are currently skipped without a diagnostic in the report.
- instruction-phrases boundaries (heuristic, by design):
  - Obfuscation is judged inside the matched phrase or destination. An invisible character
    placed right after the end of a URL or e-mail, or beyond the first 2048 URL characters,
    does not raise severity on its own (unicode-smuggling-guard still reports it).
  - Role rules require an explicit AI role, an unambiguous product name (ChatGPT, Copilot,
    Gemini...) or, for names that are also first names (Claude, Dan), a vendor prefix or a
    jailbreak-style continuation. Unusual phrasings can be missed; roleplay prose that
    matches these shapes can be flagged as medium when visible.
  - Severity is high only for hidden text, non-body channels, or obfuscated matches;
    visible instructions are medium so documents that discuss injection are not blocked.

## Tests

```bash
.venv/bin/pytest -q                       # unit + integration (skips what is not installed)
.venv/bin/pytest -q -m "not integration"  # fast unit suite
```

Corpus fixtures are generated by `tests/corpus/generate.py`; no opaque binaries are committed.
User sample documents in the repository root are git-ignored.
