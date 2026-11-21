"""clean and detect pipelines. The verdict comes only from :mod:`doc_sentinel.aggregate`."""

from __future__ import annotations

import shutil
import tempfile
import time
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
    return src.with_name(f"{src.stem}.sanitized{src.suffix}")


def pin_sanitized_output(src: Path, output: Path | None, force: bool,
                         report_path: Path | None = None):
    """Pin all protected identities before scanning an untrusted document."""
    dest = (output or default_sanitized_output(src)).resolve()
    if same_file(src, dest):
        raise UsageError("output path must differ from the input (no in-place sanitizing)")
    if report_path is not None and (same_file(report_path, src) or same_file(report_path, dest)):
        raise UsageError("report path must differ from the input and the output document")
    if (dest.exists() or dest.is_symlink()) and not force:
        raise UsageError(f"output exists: {dest} (use --force-output to replace it)")
    return dest, pin_target(dest, protect=[src] + ([report_path] if report_path else []))


def _sanitize_channels(src: Path, dtype: str):
    """Obtain complete, structurally locatable streams, never finding excerpts."""
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


def _sanitize_spans(src: Path, report: dict, plan: dict | None, review_root: Path | None):
    """Bind detections to exact sentence spans; never treat a channel as an instruction."""
    from doc_sentinel.aggregate import _norm
    from doc_sentinel.glue.instruction_phrases import InstructionPhrasesAdapter, instruction_spans

    dtype = report["input"]["detected_type"]
    channels, hidden, hidden_ranges = _sanitize_channels(src, dtype)
    spans: list[dict] = []
    covered: set[str] = set()
    adapter = InstructionPhrasesAdapter()
    findings = [f for f in report["findings"] if f.get("class") in _INJECTIONS]
    phrase_channels = {f["location"]["channel"] for f in findings
                       if "instruction-phrases" in f.get("adapter", [])}
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
                spans.append({"channel": channel.channel, "pointer": channel.pointer,
                              "text": channel.text[left:right]})
    if phrase_channels - covered:
        return [], hidden_ranges, "detected injection has no exact editable source span"

    if plan is not None and review_root is not None:
        from doc_sentinel.review import MARKER, review_fragments

        fragments = review_fragments(review_root, plan)
        for item in report.get("_sanitize_review_origins", []):
            fragment = fragments.get(item["task_id"])
            if not isinstance(fragment, str) or not fragment:
                return [], hidden_ranges, "reviewed injection has no exact editable source span"
            loc = item["origin"]
            needle = _norm(fragment.replace(MARKER, " "))
            options = [(c, sentence) for c in channels
                       if c.channel == loc["channel"] and c.pointer == loc["pointer"]
                       and MARKER not in c.text
                       for sentence in _sentences(c.text) if needle in _norm(sentence)]
            if len(options) != 1:
                if loc["hidden"] and any(c.channel == loc["channel"] and c.pointer == loc["pointer"]
                                         and MARKER not in c.text and needle in _norm(c.text) for c in hidden):
                    continue
                return [], hidden_ranges, "reviewed injection has no exact editable sentence"
            channel, sentence = options[0]
            spans.append({"channel": channel.channel, "pointer": channel.pointer, "text": sentence})
    for finding in findings:
        adapters = finding.get("adapter", [])
        if not ({"instruction-phrases", "agent-review"} & set(adapters)):
            return [], hidden_ranges, "detected injection has no exact editable source span"
    unique = []
    for span in spans:
        if span not in unique:
            unique.append(span)
    return unique, hidden_ranges, None


def run_sanitize(src: Path, dest: Path, target, report: dict, detect_code: int, *,
                 plan: dict | None = None, review_root: Path | None = None):
    """Run the engine in disposable storage and publish only after all checks pass."""
    from doc_sentinel.sanitize import SanitizeResult, sanitize

    def refused(reason):
        return SanitizeResult("refused", refusal_reason=reason)

    expected = plan["input_sha256"] if plan else report["input"]["sha256"]
    try:
        unchanged = src.is_file() and sha256_file(src) == expected
    except OSError:
        unchanged = False
    if not unchanged:
        result = refused("original input missing or changed since detection")
    elif report["verdict"] == "error":
        result = refused("detection did not complete safely")
    else:
        glyph = any(f.get("class") == "tounicode_mismatch" for f in report["findings"])
        dtype = report["input"]["detected_type"]
        hidden_ranges = []
        try:
            if glyph or dtype not in {"docx", "html", "txt", "md"}:
                spans, reason = [], None
            else:
                spans, hidden_ranges, reason = _sanitize_spans(src, report, plan, review_root)
            if reason:
                result = refused(reason)
            else:
                with tempfile.TemporaryDirectory(prefix="doc-sentinel-sanitize-") as tmp:
                    candidate = Path(tmp) / ("candidate" + src.suffix)
                    result = sanitize(src, candidate, injection_spans=spans, workdir=Path(tmp),
                                      has_glyph_divergence=glyph)
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
                                if action["technique"] == "visible-injection" and any(
                                        part == action["part"] and start < loc.get("end_byte", 0)
                                        and end > loc.get("start_byte", 0)
                                        for part, start, end in hidden_ranges):
                                    action["visibility"] = "hidden"
                            atomic_write_bytes(target, candidate.read_bytes())
        except (OSError, ValueError, TypeError, UnicodeError) as exc:
            result = refused(f"cannot sanitize safely: {exc}")
    report.pop("_sanitize_review_origins", None)
    report["sanitize"] = {"status": result.status,
                          "output_path": str(dest) if result.status == "sanitized" else None,
                          "refusal_reason": result.refusal_reason,
                          "checks": result.checks, "actions": result.actions}
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
