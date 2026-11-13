# doc-sentinel

Meta-harness that **cleans** deterministic AI-tool traces from documents and **detects**
prompt injection (hidden text, Unicode smuggling, visual-vs-extracted divergence), by
orchestrating third-party sub-harnesses pinned to exact `main` SHAs. The design and its
rationale are in [`PLAN.md`](./PLAN.md); agent rules are in [`AGENTS.md`](./AGENTS.md).

```text
doc-sentinel clean  <file> [-o PATH] [--force-output] [--json] [--report PATH]
doc-sentinel detect <file> [--json] [--report PATH] [--allow-network] [--fail-on high|medium]
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
If channel extraction fails (e.g. an archive part over the size limit), the verdict is `error`.

## Sub-harnesses

| id | Role | License | Invocation |
|----|------|---------|------------|
| watermarks-remover | clean: Layer A Unicode, metadata, embedded-media C2PA | MIT | `inspect_file.py` / `clean_file.py` scripts only (never its HTTP `/clean` or Layer B) |
| perlecta-mcp | detect: hidden text in PDF/DOCX/EML | Apache-2.0 | `perlecta analyze --json` (confined) |
| hidden-text-detector | detect: pixel contrast hidden text | MIT | `scripts/scan.py --json` |
| subtext-scan | detect: HTML hidden text + English instruction phrasing | Apache-2.0 | `node bin/subtext-scan.mjs scan --json` |
| chk_defaced-rs | detect: ToUnicode / glyph remapping, invisible text | AGPL-3.0-only | separate process only (`bin/chk_defaced`) |
| unicode-smuggling-guard | detect: tags, bidi, zero-width in extracted channels | MIT | library in a child process |
| PurpleLlama / LlamaFirewall | detect: Prompt Guard 2 classifier on channels | MIT (LlamaFirewall) | child process, offline, never AlignmentCheck |

Own deterministic glue (added only where the user's samples proved a gap): channel extractor
(filename, metadata, comments, alt, links, tracked changes, embedded names, form fields, OCR),
OOXML HTML-paste residue cleaner, OOXML library-fingerprint cleaner.

## Setup (Linux/macOS)

```bash
git submodule update --init --recursive
uv venv -p 3.12 .venv
uv pip install -p .venv --index-strategy unsafe-best-match \
  --extra-index-url https://download.pytorch.org/whl/cpu \
  -e '.[dev,detect,promptguard]' -e 'third_party/perlecta-mcp[rendu]' \
  -e third_party/unicode-smuggling-guard -e third_party/PurpleLlama/LlamaFirewall
(cd third_party/subtext-scan && npm ci --ignore-scripts)          # Node >= 22.13
CARGO_TARGET_DIR=.cache/cargo cargo build --release \
  --manifest-path third_party/chk_defaced-rs/Cargo.toml && mkdir -p bin \
  && cp .cache/cargo/release/chk_defaced bin/                      # rustc >= 1.88
.venv/bin/doc-sentinel version
```

Optional system tools: `tesseract` (+`spa`), `qpdf`, `exiftool`, `c2patool` (inspection only).
Without exiftool/c2patool, `clean` warns that metadata and C2PA verification are less reliable.

### Prompt Guard 2 (gated model)

`meta-llama/Llama-Prompt-Guard-2-86M` requires accepting the Llama license on Hugging Face.
Download it once, with consent; `detect` then runs offline from the cache:

```bash
hf auth login
hf download meta-llama/Llama-Prompt-Guard-2-86M
```

Until it is cached the `prompt-guard` adapter is reported in `adapters_skipped`, and documents
whose mandatory layers include it can never get a `clean` verdict (they get `error`, or
`findings` if other layers already found something).

## Containers

```bash
docker compose -f containers/compose.yaml run --rm detect detect /work/in/file.pdf --json
docker compose -f containers/compose.yaml run --rm clean  clean  /work/in/file.pdf -o /work/out/file.cleaned.pdf
```

Both services run with `network_mode: none`; inputs are mounted read-only.

## Known gaps (not covered, by design or by the pinned tools)

- Statistical text watermarks (SynthID-Text, textGrain) and pixel watermarks are **not** removed:
  that would require rewriting content, which is prohibited (PLAN principle 2).
- XLSX/PPTX/EPUB/ODT detect: extracted text only, no structural hidden-content analysis.
- subtext-scan phrasing rules are English; Spanish injection relies on Prompt Guard 2.
- OCR failures are currently skipped without a diagnostic in the report.

## Tests

```bash
.venv/bin/pytest -q                       # unit + integration (skips what is not installed)
.venv/bin/pytest -q -m "not integration"  # fast unit suite
```

Corpus fixtures are generated by `tests/corpus/generate.py`; no opaque binaries are committed.
User sample documents in the repository root are git-ignored.
