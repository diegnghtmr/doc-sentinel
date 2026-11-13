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
from doc_sentinel.report import atomic_write_bytes, new_report, same_file, sha256_file
from doc_sentinel.schema import AdapterOutcome, Channel, Finding


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
    """Text revealed by document-layer adapters, re-fed to the Unicode scanner + classifier."""
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
               timeout_s: float = 180.0) -> tuple[dict[str, Any], int]:
    src = _check_input(src)
    dtype = detect_type(src)
    report = new_report("detect", src, dtype)
    adapters, mandatory = registry.detect_plan(dtype)
    if not adapters:
        raise UsageError(f"unsupported format for detect: {dtype}")
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
    return report, code


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
            atomic_write_bytes(dest, current.read_bytes())
            report["output_path"] = str(dest)

    if sha256_file(src) != original_sha:
        report["warnings"].append("ORIGINAL WAS MODIFIED — this is a bug; report it")
        tool_failed = True
    report["verdict"], code = decide_clean(tool_failed=tool_failed, residual=residual,
                                           changed=changed)
    return report, code
