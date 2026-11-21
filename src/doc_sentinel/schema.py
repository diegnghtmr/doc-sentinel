"""Normalized finding schema and run report (PLAN §8).

Every adapter maps its native output to :class:`Finding`. Vocabularies are closed:
constructing a finding with an unknown ``cls``, ``severity`` or ``channel`` raises
``ValueError`` so adapter mapping mistakes fail loudly in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1
EXCERPT_MAX = 600

CLASSES = frozenset({
    "hidden_text", "unicode_smuggling", "visual_divergence", "tounicode_mismatch",
    "prompt_injection", "metadata_injection", "filename_injection", "link_or_alt_injection",
    "embedded_file", "c2pa_manifest", "document_metadata", "tool_error",
})
CHANNELS = frozenset({
    "body", "filename", "metadata", "comment", "annotation", "alt", "hyperlink", "embedded",
    "form_field", "tracked_change", "html_hidden", "image_ocr",
})
SEVERITIES = ("info", "low", "medium", "high", "critical")  # ascending
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}
CONFIDENCES = frozenset({"measured", "heuristic", "model", "declared"})


@dataclass
class Location:
    file: str
    page: int | None = None
    bbox: list[float] | None = None
    channel: str = "body"
    pointer: str | None = None

    def __post_init__(self) -> None:
        if self.channel not in CHANNELS:
            raise ValueError(f"unknown channel: {self.channel!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "page": self.page, "bbox": self.bbox,
                "channel": self.channel, "pointer": self.pointer}


@dataclass
class Finding:
    adapter: str | list[str]
    adapter_code: str
    cls: str
    severity: str
    location: Location
    excerpt: str = ""
    confidence: str = "heuristic"
    evidence: dict[str, Any] = field(default_factory=dict)
    remediation: str = "reported_only"
    finding_id: str | None = None

    def __post_init__(self) -> None:
        if self.cls not in CLASSES:
            raise ValueError(f"unknown finding class: {self.cls!r}")
        if self.severity not in SEVERITY_RANK:
            raise ValueError(f"unknown severity: {self.severity!r}")
        if self.confidence not in CONFIDENCES:
            raise ValueError(f"unknown confidence: {self.confidence!r}")
        self.excerpt = (self.excerpt or "")[:EXCERPT_MAX]

    @property
    def adapters(self) -> list[str]:
        return list(self.adapter) if isinstance(self.adapter, list) else [self.adapter]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "finding_id": self.finding_id,
            "adapter": self.adapter,
            "adapter_code": self.adapter_code,
            "class": self.cls,
            "severity": self.severity,
            "confidence": self.confidence,
            "location": self.location.to_dict(),
            "excerpt": self.excerpt,
            "evidence": self.evidence,
            "remediation": self.remediation,
        }


@dataclass
class Channel:
    """A text stream extracted from the document for the Unicode scanner and the classifier.

    ``hidden`` marks text a human reader cannot see (revealed by a document-layer adapter).
    """

    channel: str
    text: str
    pointer: str | None = None
    page: int | None = None
    hidden: bool = False
    source: str = "extract"

    def __post_init__(self) -> None:
        if self.channel not in CHANNELS:
            raise ValueError(f"unknown channel: {self.channel!r}")


@dataclass
class AdapterOutcome:
    adapter: str
    status: str  # "ok" | "error" | "skipped"
    findings: list[Finding] = field(default_factory=list)
    reason: str | None = None
    duration_ms: int = 0

    def __post_init__(self) -> None:
        if self.status not in {"ok", "error", "skipped"}:
            raise ValueError(f"unknown adapter status: {self.status!r}")
