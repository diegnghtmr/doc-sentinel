"""Sanitized-copy engine (S14); keep removal details in a separate report.

Only explicitly passed, exact and uniquely located visible injection fragments
are removed. Do not pass truncated finding excerpts. Glyph/ToUnicode divergence
must be passed explicitly; it is never repaired by guessing. A destination must
not already exist. No output is published until all applicable checks pass.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from doc_sentinel.formats import detect_type
from doc_sentinel.report import atomic_write_bytes, pin_target, same_file
from doc_sentinel.sanitize.html import actions_for_html, extract_after, plan_html
from doc_sentinel.sanitize.text import actions_for_text, apply_edits, plan_text


@dataclass
class SanitizeResult:
    status: str
    actions: list[dict[str, Any]] = field(default_factory=list)
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    refusal_reason: str | None = None


def _check(status: str, detail: str) -> dict[str, str]:
    return {"status": status, "detail": detail}


def _normalized(text: str) -> str:
    return " ".join(text.split())


def sanitize(src: Path, dest: Path, *, injection_spans: list[dict] | None = None,
             workdir: Path, has_glyph_divergence: bool = False,
             has_unlocatable_hidden: bool = False) -> SanitizeResult:
    """Create a checked sanitized copy, or refuse without modifying either path.

    ``nothing_to_do`` and refusal/unsupported statuses produce no destination.
    Set ``has_glyph_divergence`` for font mapping findings and
    ``has_unlocatable_hidden`` when detection reports hidden content the engine
    cannot locate, such as content dependent on non-inline CSS.
    """
    src, dest, workdir = Path(src), Path(dest), Path(workdir)
    try:
        if same_file(src, dest):
            return SanitizeResult("refused", refusal_reason="output path aliases the input")
        if dest.exists() or dest.is_symlink():
            return SanitizeResult("refused", refusal_reason="output path already exists")
        if not workdir.is_dir() or not dest.parent.is_dir():
            return SanitizeResult("refused", refusal_reason="work or output directory is missing")
        if has_glyph_divergence:
            return SanitizeResult("refused", refusal_reason=(
                "glyph/ToUnicode divergence lacks unambiguous mapping provenance"))
        if has_unlocatable_hidden:
            return SanitizeResult("refused", refusal_reason="detected hidden content cannot be located")
        target = pin_target(dest, protect=(src,))
        kind = detect_type(src)
        if kind not in {"txt", "md", "html"}:
            return SanitizeResult("unsupported", refusal_reason=f"sanitize unsupported for {kind}")
        original = src.read_bytes().decode("utf-8")
        spans = injection_spans if injection_spans is not None else []
        non_inline = False
        if kind == "html":
            removals, parsed = plan_html(original, spans)
            edits = [item.edit for item in removals]
            actions = actions_for_html(original, removals)
            expected = extract_after(parsed, removals)
            non_inline = parsed.non_inline
        else:
            edits = plan_text(original, spans)
            actions = actions_for_text(original, edits, kind)
            removed = bytearray(len(original))
            for edit in edits:
                removed[edit.start:edit.end] = b"\x01" * (edit.end - edit.start)
            expected = "".join(char for index, char in enumerate(original) if not removed[index])
        css_note = "; non-inline CSS is not evaluated" if non_inline else ""
        if not edits:
            return SanitizeResult("nothing_to_do", checks={
                "redetect_clean": _check("passed", "no removable text found" + css_note),
                "opens": _check("skipped", "no copy was produced"),
                "render_equal": _check("skipped", "no copy was produced"),
                "extraction_delta_ok": _check("skipped", "no copy was produced"),
            })
        output = apply_edits(original, edits)
        encoded = output.encode("utf-8")
        decoded = encoded.decode("utf-8")
        if kind == "html":
            remaining, reparsed = plan_html(decoded, [])
            extracted = extract_after(reparsed, [])
            visible_text = "".join(a.char for a in reparsed.atoms if not a.hidden and not a.comment)
        else:
            remaining = plan_text(decoded, [])
            extracted = visible_text = decoded
        delta_ok = _normalized(extracted) == _normalized(expected)
        still_present = any(span["text"] in visible_text for span in spans)
        checks = {
            "opens": _check("passed", "output decodes as UTF-8 and HTML is reparsed if applicable"),
            "render_equal": _check("skipped", "text/Markdown/HTML has no fixed page renderer"),
            "extraction_delta_ok": _check("passed" if delta_ok else "failed",
                                           "compared normalized extraction with exact edits"),
            "redetect_clean": _check("passed" if not remaining and not still_present else "failed",
                                     "rescanned supported techniques and supplied fragments" + css_note),
        }
        if not delta_ok or remaining or still_present:
            return SanitizeResult("refused", actions, checks, "output self-check failed")
        atomic_write_bytes(target, encoded)
        return SanitizeResult("sanitized", actions, checks)
    except (OSError, TypeError, ValueError, UnicodeError) as exc:
        return SanitizeResult("refused", refusal_reason=f"cannot sanitize safely: {exc}")
