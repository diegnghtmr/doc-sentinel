"""Document adapter contracts and generated, non-sensitive integration fixtures."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from doc_sentinel import registry
from doc_sentinel.adapters.base import AdapterError, RunContext

ADAPTERS = {
    "perlecta": ("perlecta", "PerlectaAdapter"),
    "hidden-text-detector": ("hidden_text_detector", "HiddenTextDetectorAdapter"),
    "subtext-scan": ("subtext_scan", "SubtextScanAdapter"),
}


def adapter_for(adapter_id):
    adapter = registry.load(adapter_id)
    assert type(adapter).__name__ == ADAPTERS[adapter_id][1], getattr(adapter, "_reason", "")
    return adapter


def fake_output(monkeypatch, adapter_id, payload, code=0, stderr=""):
    adapter = adapter_for(adapter_id)
    module = importlib.import_module(f"doc_sentinel.adapters.{ADAPTERS[adapter_id][0]}")
    calls = []

    def fake(cmd, ctx, **kwargs):
        calls.append((cmd, ctx, kwargs))
        return CompletedProcess(cmd, code, stdout=payload, stderr=stderr)

    monkeypatch.setattr(module, "run_tool", fake)
    return adapter, module, calls


@pytest.fixture
def ctx(tmp_path):
    return RunContext(workdir=tmp_path)


@pytest.fixture
def src(tmp_path):
    return tmp_path / "sample.docx"


@pytest.mark.parametrize("adapter_id", ADAPTERS)
def test_registry_contract_and_detect_only(adapter_id, src, ctx):
    adapter = adapter_for(adapter_id)
    assert adapter.id == adapter_id
    assert adapter.mode == frozenset({"detect"})
    with pytest.raises(NotImplementedError):
        adapter.clean(src, src.with_name("out.docx"), ctx)


PERLECTA_CODES = [
    ("PDF_RENDU_INVISIBLE", "hidden_text", "body"),
    ("PDF_COULEUR_FOND", "hidden_text", "body"),
    ("PDF_TAILLE_NULLE", "hidden_text", "body"),
    ("PDF_HORS_PAGE", "hidden_text", "body"),
    ("PDF_CALQUE_MASQUE", "hidden_text", "body"),
    ("PDF_OCCULTATION", "hidden_text", "body"),
    ("PDF_ACTION_AUTOMATIQUE", "hidden_text", "annotation"),
    ("PDF_LIEN_CLIQUABLE", "link_or_alt_injection", "hyperlink"),
    ("PDF_COUCHE_NUMERISEE", "hidden_text", "body"),
    ("PDF_DIVERGENCE_RENDU", "visual_divergence", "body"),
    ("CAR_INVISIBLES", "unicode_smuggling", "body"),
    ("HOMOGLYPHES", "unicode_smuggling", "body"),
    ("META_INSTRUCTION", "metadata_injection", "metadata"),
    ("DOCX_TEXTE_MASQUE", "hidden_text", "body"),
    ("EML_CONTENU_MASQUE", "hidden_text", "html_hidden"),
    ("FUTURE_CODE", "hidden_text", "body"),
]


def perlecta_anomaly(code="DOCX_TEXTE_MASQUE", gravity="majeure", **kwargs):
    return {
        "code": code, "gravite": gravity,
        "emplacement": {"page": 2, "partie": "word/document.xml", "rectangle": [1, 2, 3, 4],
                        "position_flux": 0},
        "extrait": "synthetic hidden content", "mesure": {"procede": "vanish"},
        "explication": "synthetic explanation", **kwargs,
    }


def perlecta_report(anomalies, ignored=None, truncated=False):
    return json.dumps({"anomalies": anomalies, "detecteurs_ignores": ignored or [],
                       "detecteurs_executes": ["DOCX_TEXTE_MASQUE"], "tronque": truncated})


@pytest.mark.parametrize(("code", "cls", "channel"), PERLECTA_CODES)
def test_perlecta_all_codes_and_mapping(monkeypatch, src, ctx, code, cls, channel):
    ignored = [{"nom": "PDF_DIVERGENCE_RENDU", "motif": "cannot write"}]
    payload = perlecta_report([perlecta_anomaly(code)], ignored)
    adapter, module, calls = fake_output(monkeypatch, "perlecta", payload)
    monkeypatch.setattr(module, "venv_bin", lambda name: "/test/perlecta")
    finding, = adapter.inspect(src, ctx)
    assert (finding.adapter, finding.adapter_code, finding.cls, finding.location.channel) == (
        "perlecta", code, cls, channel)
    assert finding.severity == ("info" if code == "PDF_COUCHE_NUMERISEE" else "high")
    assert finding.confidence == "measured"
    assert finding.location.file == src.name and finding.location.page == 2
    assert finding.location.bbox == [1, 2, 3, 4]
    assert "word/document.xml" in finding.location.pointer
    assert "0" in finding.location.pointer
    assert finding.excerpt == "synthetic hidden content"
    assert finding.evidence["mesure"] == {"procede": "vanish"}
    assert finding.evidence["explication"] == "synthetic explanation"
    assert finding.evidence["detecteurs_ignores"] == ignored
    if code == "FUTURE_CODE":
        assert "unknown" in finding.evidence["mapping_note"].lower()
    assert calls[0][0] == ["/test/perlecta", "analyze", str(src.resolve()), "--json"]


def test_perlecta_retries_confined_cli_with_one_cpu_when_child_is_interrupted(
        monkeypatch, src, ctx):
    adapter = adapter_for("perlecta")
    module = importlib.import_module("doc_sentinel.adapters.perlecta")
    monkeypatch.setattr(module, "venv_bin", lambda name: "/test/perlecta")
    monkeypatch.setattr(module.shutil, "which", lambda name: "/test/taskset")
    monkeypatch.setattr(module.os, "sched_getaffinity", lambda pid: {5, 8})
    calls = []

    def fake(cmd, ctx, **kwargs):
        calls.append(cmd)
        payload = (perlecta_report([], [{"nom": "*", "motif":
                   "processus interrompu par le signal 2"}], True) if len(calls) == 1
                   else perlecta_report([perlecta_anomaly()]))
        return CompletedProcess(cmd, 0, payload, "")

    monkeypatch.setattr(module, "run_tool", fake)
    finding, = adapter.inspect(src, ctx)
    assert finding.excerpt == "synthetic hidden content"
    assert calls == [["/test/perlecta", "analyze", str(src.resolve()), "--json"],
                     ["/test/taskset", "-c", "5", "/test/perlecta", "analyze",
                      str(src.resolve()), "--json"]]


def test_perlecta_minor_and_native_channels(monkeypatch, src, ctx):
    rows = [
        perlecta_anomaly("META_INSTRUCTION", "mineure"),
        perlecta_anomaly(mesure={"procede": "commentaire"}),
        perlecta_anomaly(mesure={"procede": "revision_supprimee"}),
        perlecta_anomaly(mesure={"procede": "code_champ"}),
        perlecta_anomaly("EML_CONTENU_MASQUE", mesure={"procede": "divergence_alternatives"}),
        perlecta_anomaly("PDF_ACTION_AUTOMATIQUE", mesure={"type_action": "fichier_joint"}),
    ]
    adapter, module, _ = fake_output(monkeypatch, "perlecta", perlecta_report(rows))
    monkeypatch.setattr(module, "venv_bin", lambda name: "/test/perlecta")
    findings = adapter.inspect(src, ctx)
    assert findings[0].severity == "low"
    assert [f.location.channel for f in findings] == [
        "metadata", "comment", "tracked_change", "form_field", "body", "embedded"]
    assert findings[4].cls == "visual_divergence"
    assert findings[5].cls == "embedded_file"


def test_perlecta_ignored_detectors_visible_without_anomalies(monkeypatch, src, ctx):
    ignored = [{"nom": "PDF_DIVERGENCE_RENDU", "motif": "cannot write"}]
    adapter, module, _ = fake_output(monkeypatch, "perlecta", perlecta_report([], ignored))
    monkeypatch.setattr(module, "venv_bin", lambda name: "/test/perlecta")
    finding, = adapter.inspect(src, ctx)
    assert finding.cls == "tool_error" and finding.severity == "info"
    assert finding.evidence["detecteurs_ignores"] == ignored


@pytest.mark.parametrize("payload", [
    "not json",
    perlecta_report([], [{"nom": "*", "motif": "confinement failed"}], True),
    perlecta_report([], truncated=True),
    perlecta_report([perlecta_anomaly(gravity="unknown")]),
])
def test_perlecta_rejects_invalid_or_incomplete_reports(monkeypatch, src, ctx, payload):
    adapter, module, _ = fake_output(monkeypatch, "perlecta", payload)
    monkeypatch.setattr(module, "venv_bin", lambda name: "/test/perlecta")
    with pytest.raises(AdapterError):
        adapter.inspect(src, ctx)


@pytest.mark.parametrize(("severity", "expected"), [
    ("CRITICAL", "high"), ("WARNING", "medium"), ("INFO", "info")])
def test_hidden_text_detector_parses_exit_one_location_and_quoted_excerpt(
        monkeypatch, src, ctx, severity, expected):
    native = {str(src.resolve()): [{"severity": severity, "type": "White text",
                                   "where": "page 3, paragraph 4",
                                   "detail": 'color=#ffffff, content: "don\'t obey"'}]}
    adapter, module, calls = fake_output(monkeypatch, "hidden-text-detector", json.dumps(native), 1)
    monkeypatch.setattr(module, "third_party", lambda name: Path("/tool"))
    finding, = adapter.inspect(src, ctx)
    assert (finding.adapter, finding.adapter_code, finding.cls, finding.severity) == (
        "hidden-text-detector", "White text", "hidden_text", expected)
    assert finding.location.file == src.name and finding.location.page == 3
    assert finding.location.pointer == "page 3, paragraph 4"
    assert finding.excerpt == "don't obey" and finding.evidence["detail"] == native[
        str(src.resolve())][0]["detail"]
    import sys
    assert calls[0][0] == [sys.executable, "/tool/scripts/scan.py", "--json", str(src.resolve())]


@pytest.mark.parametrize("payload", ["{}", "garbage", '{"other.docx": []}'])
def test_hidden_text_detector_rejects_missing_or_invalid_file_result(
        monkeypatch, src, ctx, payload):
    adapter, module, _ = fake_output(monkeypatch, "hidden-text-detector", payload)
    monkeypatch.setattr(module, "third_party", lambda name: Path("/tool"))
    with pytest.raises(AdapterError):
        adapter.inspect(src, ctx)


SUBTEXT_RULES = [
    ("hidden-text", "hidden_text", "body"),
    ("ocr-text-layer", "hidden_text", "body"),
    ("instruction-phrase", "prompt_injection", "body"),
    ("metadata", "metadata_injection", "metadata"),
    ("annotation", "metadata_injection", "annotation"),
    ("attachment", "embedded_file", "embedded"),
    ("javascript", "hidden_text", "annotation"),
    ("comment", "prompt_injection", "comment"),
    ("tracked-change", "prompt_injection", "tracked_change"),
    ("html-comment", "prompt_injection", "comment"),
    ("html-attribute", "link_or_alt_injection", "alt"),
    ("unicode-tags", "unicode_smuggling", "body"),
    ("bidi-control", "unicode_smuggling", "body"),
    ("zero-width", "unicode_smuggling", "body"),
    ("future-rule", "hidden_text", "body"),
]


@pytest.mark.parametrize(("rule", "cls", "channel"), SUBTEXT_RULES)
def test_subtext_all_rules_and_mapping(monkeypatch, src, ctx, rule, cls, channel):
    result = {"format": "docx", "findings": [{"rule": rule, "severity": "medium",
        "reason": "synthetic reason", "page": 2, "bbox": [1, 2, 3, 4], "text": "synthetic",
        "phrases": ["pattern"], "mechanisms": ["tiny-text"], "evidence": {"font_pt": 1},
        "location": {"part": "word/document.xml", "paragraph": 3, "attribute": "alt"}}]}
    adapter, module, calls = fake_output(monkeypatch, "subtext-scan", json.dumps(result), 1)
    monkeypatch.setattr(module.shutil, "which", lambda name: "/test/node")
    monkeypatch.setattr(module, "third_party", lambda name: Path("/tool"))
    finding, = adapter.inspect(src, ctx)
    assert (finding.adapter, finding.adapter_code, finding.cls, finding.severity) == (
        "subtext-scan", rule, cls, "medium")
    assert finding.location.file == src.name and finding.location.page == 2
    assert finding.location.bbox == [1, 2, 3, 4] and finding.location.channel == channel
    assert "word/document.xml" in finding.location.pointer and "3" in finding.location.pointer
    assert finding.excerpt == "synthetic"
    assert finding.evidence["reason"] == "synthetic reason"
    assert finding.evidence["phrases"] == ["pattern"]
    assert finding.evidence["mechanisms"] == ["tiny-text"]
    assert finding.evidence["evidence"] == {"font_pt": 1}
    if rule == "future-rule":
        assert "unknown" in finding.evidence["mapping_note"].lower()
    assert calls[0][0] == ["/test/node", "/tool/bin/subtext-scan.mjs", "scan",
                           str(src.resolve()), "--json"]


def test_subtext_html_and_attachment_channels(monkeypatch, src, ctx):
    result = {"format": "html", "findings": [
        {"rule": "hidden-text", "severity": "low", "text": "x", "location": {"line": 2}},
        {"rule": "html-attribute", "severity": "high", "text": "x",
         "location": {"element": "meta", "attribute": "content"}},
        {"rule": "instruction-phrase", "severity": "high", "text": "x",
         "location": {"attachment": "nested.txt", "inner": {"line": 1}}},
    ]}
    adapter, module, _ = fake_output(monkeypatch, "subtext-scan", json.dumps(result))
    monkeypatch.setattr(module.shutil, "which", lambda name: "/test/node")
    monkeypatch.setattr(module, "third_party", lambda name: Path("/tool"))
    findings = adapter.inspect(src, ctx)
    assert [f.location.channel for f in findings] == ["html_hidden", "metadata", "embedded"]
    assert [f.cls for f in findings] == ["hidden_text", "metadata_injection", "prompt_injection"]
    assert [f.severity for f in findings] == ["low", "high", "high"]


@pytest.mark.parametrize("payload", ["garbage", '{"error":"bad"}', '{"findings": "bad"}',
                                  '{"findings": [{"rule":"hidden-text","severity":"weird"}]}'])
def test_subtext_rejects_invalid_reports(monkeypatch, src, ctx, payload):
    adapter, module, _ = fake_output(monkeypatch, "subtext-scan", payload)
    monkeypatch.setattr(module.shutil, "which", lambda name: "/test/node")
    monkeypatch.setattr(module, "third_party", lambda name: Path("/tool"))
    with pytest.raises(AdapterError):
        adapter.inspect(src, ctx)


@pytest.mark.parametrize("adapter_id", ADAPTERS)
def test_non_finding_exit_is_an_error(monkeypatch, src, ctx, adapter_id):
    adapter, module, _ = fake_output(monkeypatch, adapter_id, "{}", 2, "tool failed")
    if adapter_id == "perlecta":
        monkeypatch.setattr(module, "venv_bin", lambda name: "/test/perlecta")
    if adapter_id == "subtext-scan":
        monkeypatch.setattr(module.shutil, "which", lambda name: "/test/node")
    with pytest.raises(AdapterError):
        adapter.inspect(src, ctx)


def test_availability_checks_real_dependencies_and_node_version(monkeypatch, tmp_path):
    perlecta = adapter_for("perlecta")
    pmodule = importlib.import_module("doc_sentinel.adapters.perlecta")
    monkeypatch.setattr(pmodule, "venv_bin", lambda name: None)
    assert not perlecta.available().available and perlecta.available().reason
    monkeypatch.setattr(pmodule, "venv_bin", lambda name: "/test/perlecta")
    assert perlecta.available().available

    hidden = adapter_for("hidden-text-detector")
    hmodule = importlib.import_module("doc_sentinel.adapters.hidden_text_detector")
    monkeypatch.setattr(hmodule, "third_party", lambda name: tmp_path)
    assert not hidden.available().available and hidden.available().reason
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/scan.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(hmodule.importlib.util, "find_spec", lambda name: None)
    assert not hidden.available().available and hidden.available().reason
    monkeypatch.setattr(hmodule.importlib.util, "find_spec", lambda name: object())
    assert hidden.available().available

    subtext = adapter_for("subtext-scan")
    smodule = importlib.import_module("doc_sentinel.adapters.subtext_scan")
    monkeypatch.setattr(smodule, "third_party", lambda name: tmp_path)
    monkeypatch.setattr(smodule.shutil, "which", lambda name: "/test/node")
    assert not subtext.available().available and subtext.available().reason
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/subtext-scan.mjs").write_text("", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    for dep in ("fflate", "htmlparser2", "pdfjs-dist"):
        (tmp_path / "node_modules" / dep).mkdir()
    monkeypatch.setattr(smodule, "run_tool", lambda *a, **k: CompletedProcess([], 0, "v22.12.0\n", ""))
    assert not subtext.available().available and subtext.available().reason
    monkeypatch.setattr(smodule, "run_tool", lambda *a, **k: CompletedProcess([], 0, "v22.13.0\n", ""))
    assert subtext.available().available


@pytest.fixture
def generated_docx(tmp_path):
    from docx import Document
    from docx.shared import Pt, RGBColor

    dirty, clean = tmp_path / "hidden.docx", tmp_path / "clean.docx"
    doc = Document()
    doc.add_paragraph("Visible ordinary paragraph.")
    run = doc.add_paragraph().add_run("white tiny synthetic text")
    run.font.color.rgb = RGBColor(255, 255, 255)
    run.font.size = Pt(1)
    run = doc.add_paragraph().add_run("vanished synthetic text")
    run.font.hidden = True
    doc.save(dirty)
    control = Document()
    control.add_paragraph("Ordinary visible document text.")
    control.save(clean)
    return dirty, clean


@pytest.fixture
def generated_pdf(tmp_path):
    from reportlab.lib.colors import black, white
    from reportlab.pdfgen.canvas import Canvas

    dirty, clean = tmp_path / "hidden.pdf", tmp_path / "clean.pdf"
    pdf = Canvas(str(dirty), pagesize=(300, 400), pageCompression=0)
    pdf.setFillColor(black)
    pdf.setFont("Helvetica", 12)
    pdf.drawString(20, 360, "Ordinary visible paragraph")
    pdf.setFillColor(white)
    pdf.drawString(20, 320, "white synthetic payload")
    pdf.setFillColor(black)
    pdf.setFont("Helvetica", 0.5)
    pdf.drawString(20, 280, "tiny synthetic payload")
    pdf.setFont("Helvetica", 12)
    pdf.drawString(400, 240, "offpage synthetic payload")
    pdf.save()
    pdf = Canvas(str(clean), pagesize=(300, 400))
    pdf.drawString(20, 360, "Ordinary visible document text.")
    pdf.save()
    return dirty, clean


@pytest.mark.integration
@pytest.mark.parametrize("adapter_id", ADAPTERS)
def test_generated_docx_hidden_runs_and_clean_control(adapter_id, generated_docx, ctx):
    adapter = adapter_for(adapter_id)
    cap = adapter.available()
    if not cap.available:
        pytest.skip(cap.reason)
    dirty, clean = generated_docx
    findings = adapter.inspect(dirty, ctx)
    revealed = " ".join(f.excerpt for f in findings if f.cls == "hidden_text")
    assert "white tiny synthetic text" in revealed
    assert "vanished synthetic text" in revealed
    assert all(f.severity not in {"high", "critical"} for f in adapter.inspect(clean, ctx))


@pytest.mark.integration
@pytest.mark.parametrize("adapter_id", ["perlecta", "hidden-text-detector", "subtext-scan"])
def test_generated_pdf_hidden_text_and_clean_control(adapter_id, generated_pdf, ctx):
    adapter = adapter_for(adapter_id)
    cap = adapter.available()
    if not cap.available:
        pytest.skip(cap.reason)
    dirty, clean = generated_pdf
    findings = adapter.inspect(dirty, ctx)
    revealed = " ".join(f.excerpt for f in findings if f.cls == "hidden_text")
    assert "white synthetic payload" in revealed
    assert "tiny synthetic payload" in revealed
    if adapter_id == "perlecta":
        assert "offpage synthetic payload" in revealed
    assert all(f.severity not in {"high", "critical"} for f in adapter.inspect(clean, ctx))


@pytest.mark.integration
def test_generated_html_display_none_and_clean_control(tmp_path, ctx):
    adapter = adapter_for("subtext-scan")
    cap = adapter.available()
    if not cap.available:
        pytest.skip(cap.reason)
    dirty, clean = tmp_path / "hidden.html", tmp_path / "clean.html"
    dirty.write_text('<p>Visible</p><div style="display:none">hidden synthetic content</div>',
                     encoding="utf-8")
    clean.write_text("<p>Ordinary visible text.</p>", encoding="utf-8")
    findings = adapter.inspect(dirty, ctx)
    assert any(f.cls == "hidden_text" and f.location.channel == "html_hidden"
               and "hidden synthetic content" in f.excerpt for f in findings)
    assert all(f.severity not in {"high", "critical"} for f in adapter.inspect(clean, ctx))
