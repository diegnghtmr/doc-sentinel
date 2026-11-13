"""Perlecta's document-layer scanner (PLAN §3, §8).

The pinned CLI's ``_analyze`` calls ``analyser_confine`` itself; invoking the
CLI therefore preserves Perlecta's resource/write/process confinement. Its
render/OCR detector can be skipped under that confinement; the native reasons
are retained even when there are no anomalous document findings.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from doc_sentinel.adapters.base import AdapterError, Capability, RunContext, run_tool, venv_bin
from doc_sentinel.schema import Finding, Location

# Complete pinned corpus v1 vocabulary; a new upstream code is explicitly noted.
_CODES = {
    "PDF_RENDU_INVISIBLE": ("hidden_text", "body"),
    "PDF_COULEUR_FOND": ("hidden_text", "body"),
    "PDF_TAILLE_NULLE": ("hidden_text", "body"),
    "PDF_HORS_PAGE": ("hidden_text", "body"),
    "PDF_CALQUE_MASQUE": ("hidden_text", "body"),
    "PDF_OCCULTATION": ("hidden_text", "body"),
    "PDF_ACTION_AUTOMATIQUE": ("hidden_text", "annotation"),
    "PDF_LIEN_CLIQUABLE": ("link_or_alt_injection", "hyperlink"),
    "PDF_COUCHE_NUMERISEE": ("hidden_text", "body"),
    "PDF_DIVERGENCE_RENDU": ("visual_divergence", "body"),
    "CAR_INVISIBLES": ("unicode_smuggling", "body"),
    "HOMOGLYPHES": ("unicode_smuggling", "body"),
    "META_INSTRUCTION": ("metadata_injection", "metadata"),
    "DOCX_TEXTE_MASQUE": ("hidden_text", "body"),
    "EML_CONTENU_MASQUE": ("hidden_text", "html_hidden"),
}


def _classification(code: str, measure: dict[str, Any]) -> tuple[str, str]:
    cls, channel = _CODES.get(code, ("hidden_text", "body"))
    if code == "DOCX_TEXTE_MASQUE":
        channel = {"commentaire": "comment", "revision_supprimee": "tracked_change",
                   "code_champ": "form_field"}.get(measure.get("procede"), channel)
    elif code == "EML_CONTENU_MASQUE" and measure.get("procede") == "divergence_alternatives":
        cls, channel = "visual_divergence", "body"
    elif code == "PDF_ACTION_AUTOMATIQUE":
        action = measure.get("type_action")
        if action in {"fichier_joint", "document_embarque"}:
            cls, channel = "embedded_file", "embedded"
        elif action in {"uri", "document_distant"}:
            cls, channel = "link_or_alt_injection", "hyperlink"
        elif action in {"envoi_formulaire", "import_donnees"}:
            channel = "form_field"
    return cls, channel


def _parse_report(raw: str, src: Path) -> list[Finding]:
    try:
        report = json.loads(raw)
        anomalies = report["anomalies"]
        ignored = report["detecteurs_ignores"]
        if not isinstance(anomalies, list) or not isinstance(ignored, list):
            raise TypeError("invalid lists")
        if report.get("tronque") or any(item.get("nom") == "*" for item in ignored):
            raise AdapterError("Perlecta analysis was incomplete or confinement failed")
        findings = []
        for item in anomalies:
            code = item["code"]
            gravity = item["gravite"]
            severity = {"majeure": "high", "mineure": "low"}[gravity]
            if code == "PDF_COUCHE_NUMERISEE":
                severity = "info"
            measure = item.get("mesure", {})
            if not isinstance(code, str) or not isinstance(measure, dict):
                raise TypeError("invalid anomaly")
            cls, channel = _classification(code, measure)
            native_location = item["emplacement"]
            part, position = native_location.get("partie"), native_location.get("position_flux")
            pointer = str(part) if part is not None else ""
            if position is not None:
                pointer += f"#position_flux={position}"
            evidence = {"mesure": measure, "explication": item.get("explication", ""),
                        "detecteurs_ignores": ignored}
            if code not in _CODES:
                evidence["mapping_note"] = "Unknown Perlecta code; mapped as hidden text."
            findings.append(Finding(
                adapter="perlecta", adapter_code=code, cls=cls, severity=severity,
                confidence="measured", excerpt=item.get("extrait", ""), evidence=evidence,
                location=Location(file=src.name, page=native_location.get("page"),
                                  bbox=native_location.get("rectangle"), channel=channel,
                                  pointer=pointer or None),
            ))
        # Finding is the only public pipeline evidence carrier. Don't silently discard
        # skipped detectors on documents for which the other detectors find nothing.
        if ignored and not findings:
            findings.append(Finding(
                adapter="perlecta", adapter_code="DETECTEURS_IGNORES", cls="tool_error",
                severity="info", confidence="declared", location=Location(file=src.name),
                evidence={"detecteurs_ignores": ignored},
            ))
        return findings
    except AdapterError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise AdapterError("invalid Perlecta JSON report") from exc


class PerlectaAdapter:
    id = "perlecta"
    mode = frozenset({"detect"})

    def available(self) -> Capability:
        if not venv_bin("perlecta"):
            return Capability(False, "perlecta executable not found")
        return Capability(True)

    def inspect(self, src: Path, ctx: RunContext) -> list[Finding]:
        executable = venv_bin("perlecta")
        if not executable:
            raise AdapterError("perlecta executable not found")
        command = [executable, "analyze", str(src.resolve()), "--json"]
        proc = run_tool(command, ctx, ok_codes=(0,))
        if proc.returncode != 0:
            raise AdapterError(f"Perlecta exited {proc.returncode}")
        # On some many-core Linux hosts, native libraries inside the 1 GiB confined
        # worker cannot create their default thread pool and abort with SIGINT.
        # Perlecta deliberately scrubs thread-count environment variables, so a
        # single CPU affinity is the only external retry that preserves its sandbox.
        try:
            report = json.loads(proc.stdout)
            interrupted = any(d.get("nom") == "*" and "signal 2" in d.get("motif", "")
                              for d in report.get("detecteurs_ignores", []))
        except (ValueError, TypeError, AttributeError):
            interrupted = False
        taskset = shutil.which("taskset") if interrupted else None
        if taskset and hasattr(os, "sched_getaffinity"):
            try:
                cpu = min(os.sched_getaffinity(0))
            except (OSError, ValueError):
                cpu = None
            if cpu is not None:
                ctx.log("retry Perlecta in one CPU after confined worker interruption")
                proc = run_tool([taskset, "-c", str(cpu), *command], ctx, ok_codes=(0,))
                if proc.returncode != 0:
                    raise AdapterError(f"Perlecta retry exited {proc.returncode}")
        return _parse_report(proc.stdout, src)

    def clean(self, src: Path, dest: Path, ctx: RunContext):
        raise NotImplementedError("perlecta is a detect-only adapter")
