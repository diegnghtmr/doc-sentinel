# Scanner and copy-safety reference

## Command surface

```text
doc-sentinel clean  FILE [-o PATH | --output-dir DIR] [--force-output] [--json | --agent-summary] [--report PATH]
doc-sentinel detect FILE [--sanitize [-o PATH | --output-dir DIR] [--force-output]] [--json | --agent-summary] [--report PATH] [--review-dir DIR] [--allow-network] [--fail-on high|medium] [--prompt-guard]
doc-sentinel sanitize FILE --review-dir DIR [-o PATH | --output-dir DIR] [--force-output] [--report PATH] [--agent-summary]
doc-sentinel review-merge --review-dir DIR [--report PATH] [--agent-summary]
doc-sentinel review-run --review-dir DIR --reviewer opencode|claude|gemini [--model M] [--json]
doc-sentinel update [--dry-run] [--only ID] [--json]
doc-sentinel version
doc-sentinel intent "<natural-language request>"
```

The last command classifies the request as `clean`, `detect`, `update`, or `detect+clean`. `detect` defaults to `--fail-on high`. A `.md` report path selects Markdown; otherwise reports use JSON. The [reviewer prompt](reviewer-prompt.md) describes the semantic batches. `review-run` is host-only: it runs the reviewer in an empty temporary directory with tools disabled, then stores raw replies under `answers/<batch_id>.json` for `review-merge` validation. OpenCode uses `github-copilot/auto` by default. The container launcher runs the host step through Python rather than passing it to the offline image.

## Scanner adapters

| Tool | Scope | License |
| --- | --- | --- |
| watermarks-remover | Layer A inspection and cleaning scripts for Unicode, metadata, and embedded-media C2PA; not HTTP or Layer B. | MIT |
| perlecta-mcp | Confined hidden-text analysis for PDF, DOCX, and EML. | Apache-2.0 |
| hidden-text-detector | Pixel-contrast hidden text. | MIT |
| subtext-scan | HTML hidden text and English instruction phrasing. | Apache-2.0 |
| chk_defaced-rs | ToUnicode and glyph remapping, plus invisible text; separate executable. | AGPL-3.0-only |
| unicode-smuggling-guard | Control and invisible-character checks in extracted channels; separate child process. | MIT |
| Built-in instruction rules | Deterministic English and Spanish channel checks, no model. | MIT |
| PurpleLlama / LlamaFirewall | Explicit opt-in local Prompt Guard 2 classification, not AlignmentCheck. | MIT for LlamaFirewall; model separately licensed |

Built-in adapters also extract metadata, filenames, comments, links, alt text, tracked changes, embedded names, form fields, and OCR; the cleaner has specific OOXML HTML-paste residue and library-fingerprint rules. The [design plan](../PLAN.md) covers the pin and update policy. Without exiftool and c2patool, the cleaner warns that metadata and C2PA verification is less reliable.

## Checked copies

`detect --sanitize` or `sanitize --review-dir` writes a sibling `<stem>.sanitized<suffix>` unless `-o` or `--output-dir` is given. A filename finding instead uses `sanitized-document` with the appropriate extension; an explicit target that reuses the untrusted name is refused. A PDF becomes a reconstructed `.docx`, and an explicit output path must have that extension. Existing targets need `--force-output`; the atomic replacement happens only after verification, so refusal preserves the original and any pre-existing target. Use the path reported in the safe summary rather than predicting it.

DOCX, HTML, TXT, and Markdown edits are conservative: visible instructions may be removed by sentence and hidden content by run or element. For a PDF finding without a bounding box, all native matches on applicable pages must be independently verified hidden before exclusion. Recognized aggregate findings require every qualifying span, including the sample, to be verified and excluded. Unlocatable or ambiguous edits and unsupported glyph mappings are refused.

Verification opens the copy, checks extraction differences, re-runs detection, and uses page rendering where applicable. For fixed-page DOCX rendering, exact pixels pass, or `equivalent_with_reflow` requires unchanged visible text apart from authorized removals, equal page counts, and at least 0.97 visual similarity on each page. Tracked changes use the final view: deletions are removed and insertions accepted. The report records actions and reasons and can include removed text; the agent summary never does. PDF reconstruction estimates layout and reading order, retains detected tables, links, and images where possible, and can turn repeated top/bottom lines into headers and footers. Scanned pages remain images without an OCR layer. Original-to-copy visual similarity is reported as a descriptive metric, not an acceptance gate.

## Detector limits

The instruction checks use phrase and role heuristics, not general language understanding. Role matches need an explicit AI role, an unambiguous product, or additional context for ambiguous names. Hidden or non-body findings and obfuscated matches may be high severity; visible instructions are medium severity, so visible prose about prompt injection can be flagged. Obfuscation is judged within the matched phrase or destination. Invisible characters just beyond a link's matched bounds or the supported length may still be reported separately by the Unicode adapter, without raising the phrase's severity. XLSX, PPTX, EPUB, and ODT receive extraction-only checks; OCR failures can be skipped. Even the optional classifier can miss findings.

On Windows, the Linux container path is required for detection. Host paths with spaces and accents are supported, and output and report directories must exist first. The PowerShell launcher tests simulate Docker; they do not prove startup and detection on a real Windows machine.
