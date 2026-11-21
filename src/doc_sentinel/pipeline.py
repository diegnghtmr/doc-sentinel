"""clean and detect pipelines. The verdict comes only from :mod:`doc_sentinel.aggregate`."""

from __future__ import annotations

import math
import shutil
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from doc_sentinel import registry
from doc_sentinel.adapters.base import RunContext
from doc_sentinel.aggregate import aggregate_detect, decide_clean
from doc_sentinel.extract import extract_channels
from doc_sentinel.formats import detect_type
from doc_sentinel.report import (
    atomic_write_bytes,
    new_report,
    pin_target,
    same_file,
    sha256_file,
)
from doc_sentinel.schema import AdapterOutcome, Channel, Finding

_INJECTIONS = frozenset({"prompt_injection", "metadata_injection", "filename_injection",
                         "link_or_alt_injection"})


class UsageError(Exception):
    """Invalid invocation (missing input, unsafe output path, unsupported format)."""


def _run_inspect(adapter, src: Path, ctx: RunContext) -> AdapterOutcome:
    cap = adapter.available()
    if not cap.available:
        return AdapterOutcome(adapter.id, "skipped", reason=cap.reason or "unavailable")
    started = time.monotonic()
    try:
        findings = adapter.inspect(src, ctx)
    except Exception as exc:  # noqa: BLE001 - adapter failure is data for the aggregator
        return AdapterOutcome(adapter.id, "error", reason=f"{type(exc).__name__}: {exc}"[:500],
                              duration_ms=int((time.monotonic() - started) * 1000))
    return AdapterOutcome(adapter.id, "ok", findings=list(findings),
                          duration_ms=int((time.monotonic() - started) * 1000))


def _hidden_channels(findings: list[Finding]) -> list[Channel]:
    """Re-feed text revealed by document-layer adapters to the channel adapters."""
    seen: set[str] = set()
    channels = []
    for finding in findings:
        text = finding.excerpt.strip()
        if finding.cls not in {"hidden_text", "visual_divergence", "tounicode_mismatch"}:
            continue
        if not text or text in seen:
            continue
        seen.add(text)
        channels.append(Channel(finding.location.channel, text, page=finding.location.page,
                                pointer=f"revealed-by:{finding.adapters[0]}", hidden=True,
                                source=finding.adapters[0]))
    return channels


def _record(report: dict[str, Any], outcome: AdapterOutcome) -> None:
    if outcome.status == "ok":
        report["adapters_run"].append(outcome.adapter)
    elif outcome.status == "skipped":
        report["adapters_skipped"].append({"id": outcome.adapter, "reason": outcome.reason})
    else:
        report["adapters_failed"].append({"id": outcome.adapter, "reason": outcome.reason})


def _check_input(src: Path) -> Path:
    if not src.is_file():
        raise UsageError(f"input not found: {src}")
    return src.resolve()


def run_detect(src: Path, *, allow_network: bool = False, fail_on: str = "high",
               timeout_s: float = 180.0, prompt_guard: bool = False,
               review_targets=None) -> tuple[dict[str, Any], int]:
    src = _check_input(src)
    dtype = detect_type(src)
    report = new_report("detect", src, dtype)
    adapters, mandatory = registry.detect_plan(dtype)
    if not adapters:
        raise UsageError(f"unsupported format for detect: {dtype}")
    if prompt_guard:
        adapters.append(registry.load("prompt-guard"))
    if dtype in registry.KNOWN_GAPS:
        report["warnings"].append(registry.KNOWN_GAPS[dtype])

    outcomes: list[AdapterOutcome] = []
    with tempfile.TemporaryDirectory(prefix="doc-sentinel-") as tmp:
        ctx = RunContext(workdir=Path(tmp), allow_network=allow_network, timeout_s=timeout_s)
        document_layer = [a for a in adapters if a.id not in registry.CHANNEL_ADAPTERS]
        channel_layer = [a for a in adapters if a.id in registry.CHANNEL_ADAPTERS]
        for adapter in document_layer:
            outcomes.append(_run_inspect(adapter, src, ctx))
        revealed = [f for o in outcomes if o.status == "ok" for f in o.findings]
        try:
            ctx.channels = extract_channels(src, dtype) + _hidden_channels(revealed)
        except Exception as exc:  # noqa: BLE001
            # The channel layer then only sees the filename and revealed text: record the
            # extraction as a failed mandatory step so the verdict can never be "clean".
            reason = f"{type(exc).__name__}: {exc}"[:500]
            report["warnings"].append(f"channel extraction failed: {reason}")
            outcomes.append(AdapterOutcome("channel-extract", "error", reason=reason))
            mandatory = mandatory | {"channel-extract"}
            ctx.channels = [Channel("filename", src.name, pointer="filename"),
                            *_hidden_channels(revealed)]
        for adapter in channel_layer:
            outcomes.append(_run_inspect(adapter, src, ctx))

    for outcome in outcomes:
        _record(report, outcome)
    verdict = aggregate_detect(outcomes, mandatory=mandatory, fail_on=fail_on)
    report["findings"] = [f.to_dict() for f in verdict.findings]
    report["verdict"], code = verdict.verdict, verdict.exit_code
    if sha256_file(src) != report["input"]["sha256"]:  # detect must be read-only
        report["warnings"].append("input changed during detect")
        report["verdict"], code = "error", 2
    if review_targets is not None:
        from doc_sentinel.review import write_plan

        write_plan(review_targets, report, code, ctx.channels, verdict.findings, mandatory, fail_on)
    return report, code


def default_sanitized_output(src: Path) -> Path:
    is_pdf = detect_type(src) == "pdf" if src.is_file() else src.suffix.lower() == ".pdf"
    suffix = ".docx" if is_pdf else src.suffix
    return src.with_name(f"{src.stem}.sanitized{suffix}")


def pin_sanitized_output(src: Path, output: Path | None, force: bool,
                         report_path: Path | None = None):
    """Pin all protected identities before scanning an untrusted document."""
    dest = (output or default_sanitized_output(src)).resolve()
    if same_file(src, dest):
        raise UsageError("output path must differ from the input (no in-place sanitizing)")
    is_pdf = detect_type(src) == "pdf" if src.is_file() else src.suffix.lower() == ".pdf"
    if is_pdf and dest.suffix.lower() != ".docx":
        raise UsageError("sanitized PDF output must end in .docx")
    if report_path is not None and (same_file(report_path, src) or same_file(report_path, dest)):
        raise UsageError("report path must differ from the input and the output document")
    if (dest.exists() or dest.is_symlink()) and not force:
        raise UsageError(f"output exists: {dest} (use --force-output to replace it)")
    return dest, pin_target(dest, protect=[src] + ([report_path] if report_path else []))


_PDF_TEXT_FINDINGS = frozenset({
    "Transparent text", "Sub-legible font size", "Text positioned outside the page",
    "Text not visible in the rendered page", "Text in invisible render mode (mode 3)",
    "Text inside a hidden optional content group (layer)",
})


_PDF_VERIFIED_HIDDEN = frozenset({"render-mode-3", "outside-cropbox", "optional-content-off",
                                   "tiny-background-match"})
_PDF_AGGREGATE_RULES = {
    "PDF.TINY_TEXT": "tiny_text", "DOCX.TINY_TEXT": "tiny_text",
    "PDF.INVISIBLE_TEXT_COLOR": "invisible_color", "DOCX.INVISIBLE_TEXT_COLOR": "invisible_color",
    "Sub-legible font size": "tiny_text", "White text": "invisible_color",
}


def _pdf_match_pages(src: Path):
    """Extract every trace span with the converter's own per-character visibility proof."""
    from doc_sentinel._pymupdf import load as _load_pymupdf
    pymupdf = _load_pymupdf()

    from doc_sentinel.sanitize.pdf_to_docx import (
        _all_layers,
        _background_colors,
        _extract_page,
        _key,
        _ocg_off,
        _without_text,
    )

    def color_candidate(char, pix):
        # Unknown backgrounds are included, not silently ruled out as visible.
        if char is None:
            return True
        rgb = ((char.color >> 16) & 255, (char.color >> 8) & 255, char.color & 255)
        if (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) / 255 > 0.9:
            return True
        colors = _background_colors(char, pix)
        if colors is None:
            return True
        return any(all(abs(rgb[i] - int(color[i])) < 0.08 * 255 for i in range(3))
                   for color in colors)

    with tempfile.TemporaryDirectory(prefix="doc-sentinel-pdf-locate-") as tmp:
        root = Path(tmp)
        layers, textless = root / "layers.pdf", root / "textless.pdf"
        off_names = _ocg_off(src)
        _all_layers(src, layers)
        _without_text(src, textless)
        with pymupdf.open(src) as original:
            counts, drawings = [], []
            for page in original:
                if page.rotation:
                    raise ValueError("rotated PDF page cannot be located")
                counts.append(Counter(_key(chr(code), origin) for trace in page.get_texttrace()
                                      for code, _gid, origin, _bbox in trace["chars"]))
                drawings.append(page.get_drawings())
        pages = []
        with pymupdf.open(layers) as pdf, pymupdf.open(textless) as background:
            for number, page in enumerate(pdf):
                pix = background[number].get_pixmap(matrix=pymupdf.Matrix(2, 2),
                                                     colorspace=pymupdf.csRGB, alpha=False)
                extracted, extras = _extract_page(page, off_names, pix, [], counts[number], drawings[number])
                raw = defaultdict(list)
                for line in extracted.lines:
                    for char in line.chars:
                        raw[_key(char.text, char.origin)].append((char, False))
                for group in extras:
                    for char in group:
                        raw[_key(char.text, char.origin)].append((char, True))
                traces = page.get_texttrace()
                occurrences = Counter(_key(chr(code), origin) for trace in traces
                                      for code, _gid, origin, _bbox in trace["chars"])
                stream, spans = [], []
                for trace in traces:
                    entries = []
                    for code, _gid, origin, _bbox in trace["chars"]:
                        key = _key(chr(code), origin)
                        choices = raw[key]
                        char, extra = choices[0] if occurrences[key] == len(choices) == 1 else (None, False)
                        entries.append((chr(code), char, extra))
                    if not entries:
                        continue
                    size = trace.get("size")
                    tiny = not isinstance(size, (int, float)) or not math.isfinite(size) or size < 4
                    spans.append({"entries": entries, "tiny_text": tiny,
                                  "invisible_color": any(color_candidate(c, pix) for _, c, _ in entries)})
                    stream.extend(entries)
                pages.append({"stream": stream, "spans": spans, "extracted": extracted})
    return pages


def _pdf_native_coverage(pages: list[dict], regions: list[dict]) -> set[int]:
    """Use the converter's exact region binding without changing independent proofs."""
    from doc_sentinel.sanitize.pdf_to_docx import _validate_spans

    regions = [region for region in regions if not region.get("auto_excluded")]
    if not regions:
        return set()
    chars = [char for page in pages for line in page["extracted"].lines for char in line.chars]
    saved = [(char, char.technique, char.reason) for char in chars]
    try:
        # Validate all characters, including those the converter already excludes.
        for char in chars:
            char.technique = ""
        _validate_spans([page["extracted"] for page in pages], regions, [])
        return {id(char) for char in chars if char.technique == "hidden-region"}
    finally:
        for char, technique, reason in saved:
            char.technique, char.reason = technique, reason


def _pdf_add_verified_regions(regions: list[dict], page: int, entries: list[tuple]) -> None:
    """Outside-crop extras are already excluded; other verified spans use converter regions."""
    from itertools import groupby

    from doc_sentinel.sanitize.pdf_to_docx import _union

    for extra, group in groupby(entries, key=lambda item: item[2]):
        chars = [item[1] for item in group]
        region = {"page": page, "bbox": _union([char.bbox for char in chars]),
                  "text": "".join(char.text for char in chars), "technique": "hidden-region"}
        if extra:
            region["auto_excluded"] = True
        if region not in regions:
            regions.append(region)


def _pdf_aggregate_criterion(finding: dict) -> str | None:
    adapters = finding.get("adapter") or []
    code = finding.get("adapter_code")
    if not adapters or adapters[0] not in {"chk_defaced", "hidden-text-detector"}:
        return None
    return _PDF_AGGREGATE_RULES.get(code)


def _pdf_aggregate_coverage(finding: dict, criterion: str, pages: list[dict],
                            regions: list[dict]) -> tuple[dict | None, str | None]:
    """Prove that every criterion candidate is already excluded, then bind the sample."""
    from doc_sentinel.sanitize import _normalized
    from doc_sentinel.sanitize.pdf_to_docx import _union

    selected = []
    for number, page in enumerate(pages, 1):
        for span in page["spans"]:
            if not span[criterion]:
                continue
            entries = span["entries"]
            if any(char is None or char.technique not in _PDF_VERIFIED_HIDDEN for _, char, _ in entries):
                return None, "PDF aggregate criterion has an unverifiable span"
            selected.append((number, entries))
    sample = finding.get("excerpt")
    if not isinstance(sample, str):
        sample = ""
    if sample.endswith("…"):
        sample = sample[:-1]
    elif sample.endswith("..."):
        sample = sample[:-3]
    needle = _normalized(sample)
    text = _normalized(" ".join("".join(entry[0] for entry in entries) for _, entries in selected))
    if not needle or needle not in text:
        return None, "PDF aggregate sample is not covered by excluded text"
    locations = []
    for number, entries in selected:
        _pdf_add_verified_regions(regions, number, entries)
        locations.append({"page": number, "bbox": _union([entry[1].bbox for entry in entries])})
    return {"finding_id": finding.get("finding_id"), "method": "covered by criterion scan",
            "criterion": criterion, "span_count": len(selected),
            "character_count": sum(len(entries) for _, entries in selected),
            "locations": locations}, None


def _pdf_native_text(finding: dict) -> str | None:
    """Only accept a native value whose adapter's own truncation limit proves completeness."""
    from doc_sentinel.adapters.hidden_text_detector import _excerpt

    adapters = finding.get("adapter") or []
    if (adapters and adapters[0] == "hidden-text-detector" and
            finding.get("adapter_code") in _PDF_TEXT_FINDINGS):
        detail = finding.get("evidence", {}).get("detail")
        text = _excerpt(detail) if isinstance(detail, str) else ""
        # The native detector stores stripped[:300]; equality to its limit is ambiguous.
        return text if text and len(text) < 300 else None
    return None


def _pdf_hidden_regions(src: Path, findings: list[dict]) -> tuple[list[dict], list[dict], str | None]:
    """Bind PDF findings to complete text and MuPDF top-left point geometry.

    Native Perlecta/subtext boxes are in bottom-left PDF user space. Every
    bboxless text match must be covered by a validated native region or be
    independently hidden. Aggregate findings require an independent exhaustive scan.
    """
    from doc_sentinel._pymupdf import load as _load_pymupdf
    pymupdf = _load_pymupdf()

    from doc_sentinel.sanitize import _normalized
    from doc_sentinel.sanitize.pdf_to_docx import _normalized_positions, _union

    regions, located, missing = [], [], []
    pages = None
    with pymupdf.open(src) as pdf:
        for finding in findings:
            if finding.get("class") not in {"hidden_text", "visual_divergence"}:
                continue
            loc = finding.get("location") or {}
            page, box, text = loc.get("page"), loc.get("bbox"), finding.get("excerpt")
            adapters = set(finding.get("adapter") or [])
            if page is not None and (type(page) is not int or not 1 <= page <= len(pdf)):
                return regions, located, "PDF hidden finding has an invalid page"
            valid_box = (isinstance(box, (list, tuple)) and len(box) == 4 and
                         all(type(x) in (int, float) and math.isfinite(x) for x in box) and
                         box[0] < box[2] and box[1] < box[3])
            if page is None or not valid_box:
                missing.append(finding)
                continue
            if (not isinstance(text, str) or not text.strip() or len(text) >= 600 or
                    not adapters or not adapters <= {"perlecta", "subtext-scan"}):
                return regions, located, "PDF hidden finding has no complete native text"
            current = pdf[page - 1]
            if current.rotation:
                return regions, located, "PDF hidden text geometry cannot be verified"
            mapped = pymupdf.Rect(box) * current.transformation_matrix
            selected = "".join(char["c"] for block in current.get_text("rawdict")["blocks"]
                               for line in block.get("lines", []) for span in line["spans"]
                               for char in span["chars"]
                               if pymupdf.Rect(char["bbox"]).intersects(mapped))
            if _normalized(text) != _normalized(selected):
                # The default rawdict omits OFF-layer and outside-CropBox text.
                # Bind those boxes to the converter's all-layer trace only when
                # every selected character is independently verified hidden.
                if pages is None:
                    pages = _pdf_match_pages(src)
                stream = pages[page - 1]["stream"]
                if any(char is None for _, char, _ in stream):
                    return regions, located, "PDF hidden text geometry cannot be verified"
                entries = [item for item in stream if pymupdf.Rect(item[1].bbox).intersects(mapped)]
                if (_normalized(text) != _normalized("".join(item[0] for item in entries)) or
                        any(item[1].technique not in _PDF_VERIFIED_HIDDEN for item in entries)):
                    return regions, located, "PDF hidden text geometry cannot be verified"
                _pdf_add_verified_regions(regions, page, entries)
            else:
                region = {"page": page, "bbox": list(mapped), "text": selected,
                          "technique": "hidden-region"}
                if region not in regions:
                    regions.append(region)
            located.append({"finding_id": finding.get("finding_id"), "method": "native_bbox",
                            "page": page, "bbox": list(mapped)})
    if not missing:
        return regions, located, None
    if pages is None:
        pages = _pdf_match_pages(src)
    try:
        native_covered = _pdf_native_coverage(pages, regions)
    except ValueError:
        return regions, located, "PDF hidden text geometry cannot be verified"
    for finding in missing:
        text = _pdf_native_text(finding)
        criterion = _pdf_aggregate_criterion(finding)
        adapters = finding.get("adapter") or []
        where = finding.get("evidence", {}).get("where", "")
        paragraph = (adapters and adapters[0] == "hidden-text-detector" and
                     isinstance(where, str) and where.lower().startswith("paragraph "))
        if paragraph or text is None or not _normalized(text):
            if criterion is None:
                return regions, located, "PDF hidden finding has no complete native text"
            coverage, reason = _pdf_aggregate_coverage(finding, criterion, pages, regions)
            if reason:
                return regions, located, reason
            located.append(coverage)
            continue
        needle = _normalized(text)
        scope = finding.get("location", {}).get("page")
        matches = []
        for number, page in enumerate(pages, 1):
            if scope is not None and number != scope:
                continue
            stream = page["stream"]
            normalized, positions = _normalized_positions("".join(item[0] for item in stream))
            start = normalized.find(needle)
            while start >= 0:
                first, last = positions[start], positions[start + len(needle) - 1] + 1
                matches.append((number, stream[first:last]))
                start = normalized.find(needle, start + 1)
        if not matches:
            return regions, located, "PDF hidden text match is missing or ambiguous"
        for number, match in matches:
            chars = [item[1] for item in match]
            if any(char is None for char in chars):
                return regions, located, "PDF hidden text geometry cannot be verified"
            if any(char.technique not in _PDF_VERIFIED_HIDDEN and id(char) not in native_covered
                   for char in chars):
                return regions, located, "PDF hidden text match is not verifiably hidden"
        for number, match in matches:
            _pdf_add_verified_regions(regions, number, match)
            located.append({"finding_id": finding.get("finding_id"), "method": "text_match",
                            "page": number, "bbox": _union([item[1].bbox for item in match])})
    return regions, located, None


def _pdf_channels(src: Path, regions: list[dict]):
    """Use full page lines and exact hidden fragments, never a finding excerpt as a span."""
    from doc_sentinel._pymupdf import load as _load_pymupdf
    pymupdf = _load_pymupdf()

    channels = [c for c in extract_channels(src, "pdf") if c.channel != "body"]
    hidden = [Channel("body", r["text"], pointer="page text", page=r["page"], hidden=True)
              for r in regions]
    from doc_sentinel.sanitize import _normalized

    with pymupdf.open(src) as pdf:
        for page in pdf:
            for block in page.get_text("dict")["blocks"]:
                lines = []
                for line in block.get("lines", []):
                    text = "".join(span["text"] for span in line["spans"])
                    for region in regions:
                        if region["page"] == page.number + 1:
                            text = text.replace(region["text"], "")
                    if text.strip():
                        lines.append(text)
                if lines:
                    channels.append(Channel("body", _normalized(" ".join(lines)),
                                            pointer="page text", page=page.number + 1))
    return channels, hidden, []


def _sanitize_channels(src: Path, dtype: str, hidden_regions: list[dict] | None = None):
    """Obtain complete, structurally locatable streams, never finding excerpts."""
    if dtype == "pdf":
        return _pdf_channels(src, hidden_regions or [])
    if dtype == "docx":
        from doc_sentinel.sanitize.docx import _raw_roots, _streams, _structural
        from doc_sentinel.sanitize.docx_zip import read_package

        _infos, entries, _comment, _raw, _directory = read_package(src)
        roots = _raw_roots(entries)
        structural = _structural(roots)
        streams = _streams(roots, entries, structural)
        channels = [Channel(s.channel, "".join(a.char for a in s.atoms), pointer=s.pointer)
                    for s in streams if s.channel in {"body", "metadata", "alt", "hyperlink", "comment"}]
        hidden = [Channel("tracked_change" if patch.technique == "tracked_deletion" else "body",
                          patch.text, pointer=patch.part, hidden=True)
                  for patch in structural if patch.text]
        hidden_ranges = [(s.part, a.start, a.end) for s in streams if s.channel != "body"
                         for a in s.atoms]
        return channels, hidden, hidden_ranges
    if dtype in {"txt", "md"}:
        text = src.read_bytes().decode("utf-8")
        return [Channel("body", line, pointer="body") for line in text.splitlines(keepends=True)
                if line.strip()], [], []
    if dtype == "html":
        from doc_sentinel.sanitize.html import _parse

        parsed = _parse(src.read_bytes().decode("utf-8"))
        channels = [Channel("body", line, pointer="body")
                    for segment in parsed.visible_segments()
                    for line in segment.splitlines(keepends=True) if line.strip()]
        hidden = [Channel("html_hidden", item.removed_text, pointer="hidden", hidden=True)
                  for item in parsed.ranges if item.removed_text]
        for kind, pointer, atoms in parsed.attributes:
            value = "".join(atom.char for atom in atoms)
            if not value:
                continue
            channel = Channel(kind, value, pointer=pointer, hidden=any(a.hidden for a in atoms))
            (hidden if channel.hidden else channels).append(channel)
        return channels, hidden, []
    return [], [], []


def _sentence_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """The smallest sentence containing a matched source span, retaining surrounding space."""
    left = start
    while left and text[left - 1] not in ".!?…\r\n¿¡":
        left -= 1
    if left and text[left - 1] in "¿¡":
        left -= 1
    while left < start and text[left].isspace():
        left += 1
    right = end
    while right < len(text) and text[right] not in ".!?…\r\n":
        right += 1
    while right < len(text) and text[right] in ".!?…":
        right += 1
    return left, right


def _sentences(text: str):
    pos = 0
    while pos < len(text):
        while pos < len(text) and (text[pos].isspace() or text[pos] in ".!?…"):
            pos += 1
        if pos == len(text):
            break
        start, end = _sentence_bounds(text, pos, pos + 1)
        yield text[start:end]
        pos = end


def _sanitize_spans(src: Path, report: dict, plan: dict | None, review_root: Path | None,
                    hidden_regions: list[dict] | None = None):
    """Bind detections to exact sentence spans; never treat a channel as an instruction."""
    from doc_sentinel.aggregate import _norm
    from doc_sentinel.glue.instruction_phrases import InstructionPhrasesAdapter, instruction_spans

    dtype = report["input"]["detected_type"]
    channels, hidden, hidden_ranges = _sanitize_channels(src, dtype, hidden_regions)
    spans: list[dict] = []
    covered: set[str] = set()
    adapter = InstructionPhrasesAdapter()
    findings = [f for f in report["findings"] if f.get("class") in _INJECTIONS]
    phrase_channels = {f["location"]["channel"] for f in findings
                       if "instruction-phrases" in f.get("adapter", [])
                       and f["location"]["channel"] != "filename"}
    with tempfile.TemporaryDirectory(prefix="doc-sentinel-span-") as tmp:
        for channel in [*channels, *hidden]:
            if channel.channel not in phrase_channels and not channel.hidden:
                continue
            ctx = RunContext(workdir=Path(tmp), channels=[channel])
            if not adapter.inspect(src, ctx):
                continue
            covered.add(channel.channel)
            if channel.hidden:
                if channel.channel == "body":
                    covered.add("body")
                continue  # Hidden runs/elements are already removed as structural units.
            for start, end in instruction_spans(channel.text):
                if any(char in "\r\n" for char in channel.text[start:end]):
                    return [], hidden_ranges, "detected injection crosses a sentence boundary"
                left, right = _sentence_bounds(channel.text, start, end)
                span = {"channel": channel.channel, "pointer": channel.pointer,
                        "text": channel.text[left:right]}
                if dtype == "pdf":
                    span["page"] = channel.page
                    if channel.channel == "metadata" and span["text"] != channel.text:
                        return [], hidden_ranges, "PDF metadata injection is not an entire value"
                spans.append(span)
    if phrase_channels - covered:
        return [], hidden_ranges, "detected injection has no exact editable source span"

    handled = []
    if plan is not None and review_root is not None:
        from doc_sentinel.review import HIDDEN_CLASSES, MARKER, review_fragments

        fragments = review_fragments(review_root, plan)
        for item in report.get("_sanitize_review_origins", []):
            fragment = fragments.get(item["task_id"])
            if not isinstance(fragment, str) or not fragment:
                return [], hidden_ranges, "reviewed injection has no exact editable source span"
            loc = item["origin"]
            source_finding = item.get("source_finding") or {}
            needle = _norm(fragment.replace(MARKER, " "))
            if loc["channel"] == "filename":
                handled.append(source_finding)
                continue  # The CLI guards the output name separately.
            if loc["hidden"] or source_finding.get("class") in HIDDEN_CLASSES:
                texts = [c.text for c in hidden if MARKER not in c.text]
                if not any(needle in _norm(text) for text in [*texts, "".join(texts), " ".join(texts)]):
                    return [], hidden_ranges, "reviewed hidden content cannot be located"
                handled.append(source_finding)
                continue
            if dtype == "docx" and loc["channel"] == "body":
                from doc_sentinel.sanitize.docx import reviewed_mixed_spans

                mixed = reviewed_mixed_spans(src, loc["pointer"], fragment.replace(MARKER, " "))
                if mixed is not None:
                    spans.extend(mixed)
                    handled.append(source_finding)
                    continue
            options = [(c, sentence) for c in channels
                       if c.channel == loc["channel"] and c.pointer == loc["pointer"]
                       and (dtype != "pdf" or c.page == loc["page"])
                       and MARKER not in c.text
                       for sentence in _sentences(c.text) if needle in _norm(sentence)]
            if len(options) != 1:
                return [], hidden_ranges, "reviewed injection has no exact editable sentence"
            channel, sentence = options[0]
            span = {"channel": channel.channel, "pointer": channel.pointer, "text": sentence}
            if dtype == "pdf":
                span["page"] = channel.page
                if channel.channel == "metadata" and sentence != channel.text:
                    return [], hidden_ranges, "PDF metadata injection is not an entire value"
            spans.append(span)
            handled.append(source_finding)
    for finding in findings:
        adapters = finding.get("adapter", [])
        if not ({"instruction-phrases", "agent-review"} & set(adapters)):
            matched = any(source and source.get("class") == finding.get("class") and
                          source.get("excerpt") == finding.get("excerpt") and
                          source.get("location") == finding.get("location") for source in handled)
            if not matched:
                return [], hidden_ranges, "detected injection has no exact editable source span"
    unique = []
    for span in spans:
        if span not in unique:
            unique.append(span)
    return unique, hidden_ranges, None


def has_filename_injection(report: dict) -> bool:
    return (any(item.get("class") == "filename_injection" for item in report.get("findings", [])) or
            any(item.get("origin", {}).get("channel") == "filename"
                for item in report.get("_sanitize_review_origins", [])))


def run_sanitize(src: Path, dest: Path, target, report: dict, detect_code: int, *,
                 plan: dict | None = None, review_root: Path | None = None):
    """Run the engine in disposable storage and publish only after all checks pass."""
    from doc_sentinel.sanitize import SanitizeResult, sanitize

    def refused(reason):
        return SanitizeResult("refused", refusal_reason=reason)

    expected = plan["input_sha256"] if plan else report["input"]["sha256"]
    located_findings = []
    try:
        unchanged = src.is_file() and sha256_file(src) == expected
    except OSError:
        unchanged = False
    if not unchanged:
        result = refused("original input missing or changed since detection")
    elif report["verdict"] == "error":
        result = refused("detection did not complete safely")
    elif has_filename_injection(report) and src.stem.casefold() in dest.name.casefold():
        result = refused("output name reuses the injection-bearing input filename")
    else:
        glyph = any(f.get("class") == "tounicode_mismatch" for f in report["findings"])
        dtype = report["input"]["detected_type"]
        hidden_ranges = []
        try:
            regions, location_reason = [], None
            if dtype == "pdf" and not glyph:
                regions, located_findings, location_reason = _pdf_hidden_regions(src, report["findings"])
            unlocatable = location_reason is not None
            if glyph or unlocatable or dtype not in {"pdf", "docx", "html", "txt", "md"}:
                spans, reason = [], None
            else:
                spans, hidden_ranges, reason = _sanitize_spans(src, report, plan, review_root, regions)
            pdf_metadata = []
            if dtype == "pdf" and not reason:
                for span in spans:
                    if span["channel"] == "metadata":
                        pdf_metadata.append({"format": "pdf", "part": "metadata", "location": None,
                                             "page": None, "bbox": None,
                                             "technique": "metadata-injection", "visibility": "hidden",
                                             "reason": "reviewed or detected injection in PDF metadata",
                                             "removed_text": span["text"]})
                    elif span["channel"] not in {"body", "hyperlink"}:
                        reason = "PDF injection channel cannot be reconstructed safely"
                        break
            if reason:
                result = refused(reason)
            else:
                with tempfile.TemporaryDirectory(prefix="doc-sentinel-sanitize-") as tmp:
                    candidate = Path(tmp) / (dest.name if dtype == "pdf" or has_filename_injection(report)
                                                 else "candidate" + src.suffix)
                    engine_spans = ([{k: span[k] for k in ("channel", "page", "text")}
                                     for span in spans if span["channel"] != "metadata"]
                                    if dtype == "pdf" else spans)
                    engine_regions = [region for region in regions if not region.get("auto_excluded")]
                    result = sanitize(src, candidate, injection_spans=engine_spans, workdir=Path(tmp),
                                      hidden_regions=engine_regions, has_glyph_divergence=glyph,
                                      has_unlocatable_hidden=unlocatable)
                    if unlocatable and result.status == "refused":
                        result.refusal_reason = location_reason
                    if result.status == "nothing_to_do" and has_filename_injection(report):
                        shutil.copyfile(src, candidate)
                        result = SanitizeResult("sanitized", actions=[{
                            "format": dtype, "part": "filename", "location": None,
                            "technique": "filename-injection", "visibility": "hidden",
                            "reason": "the injection-bearing original filename was not reused",
                            "removed_text": src.name}], checks=result.checks)
                    if dtype == "pdf":
                        for action in result.actions:
                            action["page"] = int(action["part"].split()[-1])
                            action["bbox"] = action["location"]
                        similarity = result.checks.get("visual_similarity")
                        if isinstance(similarity, dict):
                            result.checks["visual_similarity"] = [p["similarity"] for p in similarity["pages"]]
                        if result.status == "sanitized":
                            result.actions.extend(pdf_metadata)
                            if pdf_metadata and any(
                                    action["removed_text"] in channel.text
                                    for action in pdf_metadata for channel in extract_channels(candidate, "docx")):
                                result.status = "refused"
                                result.refusal_reason = "PDF metadata injection remains in reconstructed output"
                    has_hidden = any(f.get("class") in {"hidden_text", "visual_divergence"}
                                     for f in report["findings"])
                    if result.status == "nothing_to_do" and (has_hidden or spans):
                        result = refused("detected hidden content cannot be located")
                    if result.status == "sanitized":
                        # Do not publish until the normal detection pipeline also succeeds.
                        rescanned, _ = run_detect(candidate)
                        remaining = [f for f in rescanned["findings"]
                                     if f.get("class") in _INJECTIONS | {
                                         "hidden_text", "visual_divergence", "tounicode_mismatch"}]
                        check_ok = (rescanned["verdict"] != "error" and not remaining and
                                    not rescanned["adapters_failed"])
                        result.checks["redetect_clean"] = {"status": "passed" if check_ok else "failed",
                                                           "detail": "re-ran deterministic detection"}
                        if not check_ok:
                            result.status = "refused"
                            result.refusal_reason = "detected content remains or re-detection failed"
                        elif sha256_file(src) != expected:
                            result.status = "refused"
                            result.refusal_reason = "original input missing or changed since detection"
                        else:
                            for action in result.actions:
                                loc = action.get("location", {})
                                if isinstance(loc, dict) and action["technique"] == "visible-injection" and any(
                                        part == action["part"] and start < loc.get("end_byte", 0)
                                        and end > loc.get("start_byte", 0)
                                        for part, start, end in hidden_ranges):
                                    action["visibility"] = "hidden"
                            atomic_write_bytes(target, candidate.read_bytes())
        except (OSError, ValueError, TypeError, UnicodeError) as exc:
            result = refused(f"cannot sanitize safely: {exc}")
    if result.status == "sanitized" and has_filename_injection(report) and not any(
            "original filename was not reused" in warning for warning in report["warnings"]):
        report["warnings"].append("original filename was not reused because it was marked as an injection")
    report.pop("_sanitize_review_origins", None)
    report["sanitize"] = {"status": result.status,
                          "output_path": str(dest) if result.status == "sanitized" else None,
                          "refusal_reason": result.refusal_reason,
                          "checks": result.checks, "actions": result.actions}
    if report["input"]["detected_type"] == "pdf":
        report["sanitize"]["located_findings"] = located_findings
    return report, 2 if result.status in {"refused", "unsupported"} else detect_code


def default_output(src: Path) -> Path:
    return src.with_name(f"{src.stem}.cleaned{src.suffix}")


def run_clean(src: Path, *, output: Path | None = None, force_output: bool = False,
              report_path: Path | None = None,
              timeout_s: float = 300.0) -> tuple[dict[str, Any], int]:
    src = _check_input(src)
    dest = (output or default_output(src)).resolve()
    if dest == src or same_file(dest, src):
        raise UsageError("output path must differ from the input (no in-place cleaning)")
    if report_path is not None and (same_file(report_path, src)
                                    or same_file(report_path, dest)):
        raise UsageError("report path must differ from the input and the output document")
    if dest.exists() and not force_output:
        raise UsageError(f"output exists: {dest} (use --force-output to replace it)")
    target = pin_target(dest, protect=[src])
    dtype = detect_type(src)
    chain = registry.clean_chain(dtype)
    if not chain:
        raise UsageError(f"unsupported format for clean: {dtype}")

    report = new_report("clean", src, dtype)
    report["clean_actions"] = []
    original_sha = report["input"]["sha256"]
    tool_failed = changed = residual = False
    with tempfile.TemporaryDirectory(prefix="doc-sentinel-") as tmp:
        ctx = RunContext(workdir=Path(tmp), timeout_s=timeout_s)
        current = src
        for step, adapter in enumerate(chain):
            cap = adapter.available()
            if not cap.available:
                report["adapters_failed"].append({"id": adapter.id, "reason": cap.reason})
                tool_failed = True
                break
            out = Path(tmp) / f"step{step}{src.suffix}"
            try:
                result = adapter.clean(current, out, ctx)
            except Exception as exc:  # noqa: BLE001
                report["adapters_failed"].append(
                    {"id": adapter.id, "reason": f"{type(exc).__name__}: {exc}"[:500]})
                tool_failed = True
                break
            if not out.is_file():
                shutil.copyfile(current, out)
            report["adapters_run"].append(adapter.id)
            report["clean_actions"] += [{"adapter": adapter.id, **a} for a in result.actions]
            report["warnings"] += result.warnings
            changed = changed or result.changed
            residual = residual or result.residual
            if step == 0 or report["c2pa"]["before"] is None:
                report["c2pa"]["before"] = result.c2pa_before
            if result.c2pa_after is not None:
                report["c2pa"]["after"] = result.c2pa_after
            report["c2pa"]["actions"] += [{"adapter": adapter.id, **a} for a in result.actions
                                          if a.get("c2pa")]
            current = out
        if not tool_failed:
            atomic_write_bytes(target, current.read_bytes())
            report["output_path"] = str(dest)

    if sha256_file(src) != original_sha:
        report["warnings"].append("ORIGINAL WAS MODIFIED — this is a bug; report it")
        tool_failed = True
    report["verdict"], code = decide_clean(tool_failed=tool_failed, residual=residual,
                                           changed=changed)
    return report, code
