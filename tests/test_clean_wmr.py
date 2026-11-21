"""S3: deterministic watermarks-remover adapter contract and real CLI smoke tests."""
from __future__ import annotations

import ast
import hashlib
import importlib
import json
import struct
import sys
import zipfile
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from doc_sentinel.adapters.base import AdapterError, RunContext, run_tool


def _module():
    return importlib.import_module("doc_sentinel.adapters.watermarks_remover")


def _adapter():
    return _module().WatermarksRemoverAdapter()


def _ctx(tmp_path):
    return RunContext(workdir=tmp_path)


def test_contract_forbidden_entrypoints_and_flags_absent():
    path = Path("src/doc_sentinel/adapters/watermarks_remover.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tokens = [node.value for node in ast.walk(tree)
              if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    names = [node.id for node in ast.walk(tree) if isinstance(node, ast.Name)]
    forbidden = ("server.py", "POST /clean", "rewrite_text.py", "humanize_pass.py",
                 "Layer B", "--in-place")
    assert not any(item.casefold() in token.casefold() for item in forbidden for token in tokens)
    assert not any(item in names for item in ("server", "rewrite_text", "humanize_pass"))


def test_child_environment_does_not_inherit_rewrite_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("WATERMARKS_REWRITE_MODEL", "should-not-appear")
    monkeypatch.setenv("WATERMARKS_REWRITE_OTHER", "should-not-appear")
    monkeypatch.setenv("WATERMARKS_CLEAN_STRATEGY_FILE", "should-not-appear")
    proc = run_tool([sys.executable, "-c", "import json,os; print(json.dumps(dict(os.environ)))"],
                    _ctx(tmp_path), ok_codes=(0,))
    env = json.loads(proc.stdout)
    assert not any(key.startswith("WATERMARKS_REWRITE_") for key in env)
    assert "WATERMARKS_CLEAN_STRATEGY_FILE" not in env
    assert env["HF_HUB_OFFLINE"] == env["TRANSFORMERS_OFFLINE"] == "1"


def test_capability_checks_scripts_and_clean_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("DOC_SENTINEL_HOME", str(tmp_path))
    adapter = _adapter()
    assert adapter.id == "watermarks-remover"
    assert adapter.mode == frozenset({"clean"})
    cap = adapter.available()
    assert not cap.available and cap.reason
    src = tmp_path / "input.txt"
    src.write_text("safe", encoding="utf-8")
    with pytest.raises(ValueError):
        adapter.clean(src, src, _ctx(tmp_path))
    assert src.read_text(encoding="utf-8") == "safe"


def test_normalized_findings_for_inspection(monkeypatch, tmp_path):
    mod = _module()
    src = tmp_path / "input.docx"
    src.write_bytes(b"fixture")
    payload = {"kind": "container", "has_c2pa": True, "has_ai_metadata": True,
               "layer_a_hits": [{"codepoint": "U+200B", "count": 2, "kind": "strip"}]}
    calls = []

    def fake_run(cmd, ctx, **kwargs):
        calls.append(list(map(str, cmd)))
        return SimpleNamespace(returncode=1, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(mod, "run_tool", fake_run)
    found = _adapter().inspect(src, _ctx(tmp_path))
    assert {f.cls for f in found} == {"c2pa_manifest", "document_metadata", "unicode_smuggling"}
    assert all(f.adapter == "watermarks-remover" and f.severity == "info" for f in found)
    assert {f.location.channel for f in found} == {"body", "metadata"}
    assert any(f.evidence.get("count") == 2 for f in found)
    assert len(calls) == 1 and calls[0][0] == sys.executable
    assert calls[0][1].endswith("/inspect_file.py") and calls[0][2:] == ["--json", str(src)]


@pytest.mark.parametrize("after,expected_residual", [
    ({"has_c2pa": False, "has_ai_metadata": False, "layer_a_hits": []}, False),
    ({"has_c2pa": True, "has_ai_metadata": True, "layer_a_hits": []}, True),
    ({"has_c2pa": False, "has_ai_metadata": True, "layer_a_hits": []}, True),
    ({"has_c2pa": False, "has_ai_metadata": False,
      "layer_a_hits": [{"codepoint": "U+200B", "count": 1}]}, True),
])
def test_reports_c2pa_and_post_clean_residual(monkeypatch, tmp_path, after, expected_residual):
    mod = _module()
    src, dest = tmp_path / "in.docx", tmp_path / "out.docx"
    src.write_bytes(b"original")
    calls = []
    before = {"kind": "container", "has_c2pa": True, "has_ai_metadata": True,
              "layer_a_hits": [], "findings": ["word/media/image1.png: c2pa: c2pa"]}
    after = {"kind": "container", **after}

    def fake_run(cmd, ctx, **kwargs):
        argv = list(map(str, cmd))
        calls.append(argv)
        if argv[1].endswith("/inspect_file.py"):
            return SimpleNamespace(returncode=1 if argv[-1] == str(src) or expected_residual else 0,
                                   stdout=json.dumps(before if argv[-1] == str(src) else after),
                                   stderr="")
        dest.write_bytes(b"cleaned")
        payload = {"kind": "container", "changed": True,
                   "actions": ["clean embedded media in word/media/image1.png (drop PNG tEXt)"],
                   "still_has_c2pa": after["has_c2pa"],
                   "still_has_ai_metadata": after["has_ai_metadata"]}
        return SimpleNamespace(returncode=1 if after["has_c2pa"] else 0,
                               stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(mod, "run_tool", fake_run)
    result = _adapter().clean(src, dest, _ctx(tmp_path))
    assert result.changed and result.residual is expected_residual
    assert result.c2pa_before is True and result.c2pa_after is after["has_c2pa"]
    assert any(a.get("c2pa") is True for a in result.actions)
    if after["has_c2pa"]:
        assert any(a.get("c2pa") is True and a["action"] == "residual" for a in result.actions)
    else:
        assert any(a.get("c2pa") is True and a["action"] in ("removed", "cleaned")
                   for a in result.actions)
    assert len(calls) == 3
    assert all(argv[0] == sys.executable for argv in calls)
    assert calls[0][1].endswith("/inspect_file.py") and calls[0][2:] == ["--json", str(src)]
    assert calls[1][1].endswith("/clean_file.py")
    assert calls[1][2:6] == [str(src), "-o", str(dest), "--json"]
    assert calls[2][1].endswith("/inspect_file.py") and calls[2][2:] == ["--json", str(dest)]
    assert src.read_bytes() == b"original"


def test_invalid_subtool_json_is_not_treated_as_clean(monkeypatch, tmp_path):
    mod = _module()
    src = tmp_path / "in.txt"
    src.write_text("hello", encoding="utf-8")
    monkeypatch.setattr(mod, "run_tool", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="not json", stderr=""))
    with pytest.raises(AdapterError):
        _adapter().inspect(src, _ctx(tmp_path))


def _require_real_wmr():
    adapter = _adapter()
    cap = adapter.available()
    if not cap.available:
        pytest.skip(cap.reason or "watermarks-remover unavailable")
    return adapter


@pytest.mark.integration
def test_real_text_clean_smoke_and_missing_tool_warnings(tmp_path, monkeypatch):
    adapter = _require_real_wmr()
    src, dest = tmp_path / "sample.txt", tmp_path / "cleaned.txt"
    src.write_text("Hola\u202fmundo\u200b!", encoding="utf-8")
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    findings = adapter.inspect(src, _ctx(tmp_path))
    assert {f.cls for f in findings} == {"unicode_smuggling"}
    result = adapter.clean(src, dest, _ctx(tmp_path))
    assert dest.read_text(encoding="utf-8") == "Hola mundo!"
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before
    assert result.changed and not result.residual
    assert any(a["action"] == "removed" and a["count"] == 1 for a in result.actions)
    assert any(a["action"] == "replaced" and a["count"] == 1 for a in result.actions)
    assert adapter.inspect(dest, _ctx(tmp_path)) == []
    for tool in ("exiftool", "c2patool"):
        if __import__("shutil").which(tool) is None:
            assert any(tool in warning for warning in result.warnings)


@pytest.mark.integration
def test_real_docx_ai_metadata_cleaned_or_residual(tmp_path):
    docx = pytest.importorskip("docx")
    adapter = _require_real_wmr()
    src, dest = tmp_path / "input.docx", tmp_path / "out.docx"
    doc = docx.Document()
    doc.add_paragraph("Visible content about ChatGPT — unchanged.")
    doc.core_properties.author = "ChatGPT"
    doc.core_properties.comments = "Generated by AI"
    doc.save(src)
    original = hashlib.sha256(src.read_bytes()).hexdigest()
    result = adapter.clean(src, dest, _ctx(tmp_path))
    post = adapter.inspect(dest, _ctx(tmp_path))
    assert result.residual or not any(f.cls == "document_metadata" for f in post)
    assert docx.Document(dest).paragraphs[0].text == "Visible content about ChatGPT — unchanged."
    assert hashlib.sha256(src.read_bytes()).hexdigest() == original
    assert result.changed


@pytest.mark.integration
def test_real_embedded_png_c2pa_is_removed_or_explicitly_residual(tmp_path):
    docx = pytest.importorskip("docx")
    adapter = _require_real_wmr()

    def chunk(kind, payload):
        data = kind + payload
        return struct.pack(">I", len(payload)) + data + struct.pack(">I", zlib.crc32(data))

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
           + chunk(b"tEXt", b"c2pa\x00ContentCredentials")
           + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00")) + chunk(b"IEND", b""))
    image = tmp_path / "image.png"
    image.write_bytes(png)
    src, dest = tmp_path / "in.docx", tmp_path / "out.docx"
    doc = docx.Document()
    doc.add_paragraph("Ordinary visible content")
    doc.add_picture(str(image))
    doc.save(src)
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    assert any(f.cls == "c2pa_manifest" for f in adapter.inspect(src, _ctx(tmp_path)))
    result = adapter.clean(src, dest, _ctx(tmp_path))
    assert result.c2pa_before is True
    assert result.residual or result.c2pa_after is False
    assert any(a.get("c2pa") for a in result.actions)
    with zipfile.ZipFile(dest) as archive:
        media = [archive.read(name) for name in archive.namelist() if name.startswith("word/media/")]
    assert media and (result.residual or all(b"ContentCredentials" not in data for data in media))
    assert docx.Document(dest).paragraphs[0].text == "Ordinary visible content"
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["pptx", "xlsx"])
def test_real_office_media_metadata_and_unicode_cleaned_or_residual(tmp_path, kind):
    adapter = _require_real_wmr()

    def chunk(tag, value):
        data = tag + value
        return struct.pack(">I", len(value)) + data + struct.pack(">I", zlib.crc32(data))

    if kind == "pptx":
        media = b"\xff\xd8\xff\xeb" + struct.pack(">H", 18) + b"JUMB c2pa marker" + b"\xff\xd9"
        media_name = "ppt/media/image1.jpeg"
        body_name = "ppt/slides/slide1.xml"
        body = ('<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:t>Hola\u200dmundo — ChatGPT'
                '</a:t></p:sld>').encode()
    else:
        media = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
                 + chunk(b"tEXt", b"c2pa\x00ContentCredentials")
                 + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00")) + chunk(b"IEND", b""))
        media_name = "xl/media/image1.png"
        body_name = "xl/sharedStrings.xml"
        body = '<sst><si><t>Hola\u200bmundo — ChatGPT</t></si></sst>'.encode()
    src, dest = tmp_path / f"in.{kind}", tmp_path / f"out.{kind}"
    core = (b'<cp:coreProperties xmlns:cp="urn:cp" xmlns:dc="urn:dc">'
            b'<dc:creator>ChatGPT</dc:creator></cp:coreProperties>')
    app = b'<Properties><Application>Anthropic AI</Application><Company>OpenAI</Company></Properties>'
    with zipfile.ZipFile(src, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("docProps/core.xml", core)
        archive.writestr("docProps/app.xml", app)
        archive.writestr(body_name, body)
        archive.writestr(media_name, media)
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    findings = adapter.inspect(src, _ctx(tmp_path))
    assert {"c2pa_manifest", "document_metadata", "unicode_smuggling"} <= {f.cls for f in findings}
    result = adapter.clean(src, dest, _ctx(tmp_path))
    assert result.c2pa_before and any(a.get("c2pa") for a in result.actions)
    assert result.residual or result.c2pa_after is False
    with zipfile.ZipFile(dest) as archive:
        post_media = archive.read(media_name)
        post_body = archive.read(body_name)
        props = archive.read("docProps/core.xml") + archive.read("docProps/app.xml")
    assert "Holamundo — ChatGPT".encode() in post_body
    assert not any(marker in post_body for marker in ("\u200b".encode(), "\u200d".encode()))
    assert result.residual or not any(marker in post_media.lower() for marker in
                                      (b"c2pa", b"jumb", b"contentcredentials"))
    assert result.residual or not any(marker in props for marker in
                                      (b"ChatGPT", b"Anthropic", b"OpenAI"))
    assert hashlib.sha256(src.read_bytes()).hexdigest() == digest


@pytest.fixture
def without_exiftool(tmp_path, monkeypatch):
    """Hide optional programs in both the adapter and its child, retaining the sandbox."""
    import shutil

    tools = tmp_path / "tools"
    tools.mkdir()
    unshare = shutil.which("unshare")
    if unshare:
        (tools / "unshare").symlink_to(unshare)
    monkeypatch.setenv("PATH", str(tools))


def _pdf_with_metadata(tmp_path, *, producer="ChatGPT", creator="Microsoft 365 Copilot",
                       xmp=True):
    pikepdf = pytest.importorskip("pikepdf")
    canvas = pytest.importorskip("reportlab.pdfgen.canvas")
    source = tmp_path / "base.pdf"
    doc = canvas.Canvas(str(source))
    doc.drawString(72, 720, "Visible text: ChatGPT is discussed here.")
    doc.showPage()
    doc.save()
    target = tmp_path / "input.pdf"
    with pikepdf.open(source) as pdf:
        pdf.docinfo["/Producer"] = producer
        pdf.docinfo["/Creator"] = creator
        if xmp:
            xml = (b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
                   b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
                   b'<rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/" '
                   b'xmp:CreatorTool="Microsoft 365 Copilot"/>'
                   b'</rdf:RDF></x:xmpmeta>')
            pdf.Root.Metadata = pdf.make_stream(xml)
            pdf.Root.Metadata.Type = pikepdf.Name.Metadata
            pdf.Root.Metadata.Subtype = pikepdf.Name.XML
        # Do not auto-wrap the XML in an xpacket: WMR's byte-only fallback
        # misses this valid stream, so independent XMP inspection is exercised.
        pdf.save(target, compress_streams=False, fix_metadata_version=False)
    return target


def _pdf_text(path):
    fitz = pytest.importorskip("pymupdf")
    with fitz.open(path) as pdf:
        return [page.get_text() for page in pdf]


@pytest.mark.integration
def test_pdf_fallback_removes_ai_info_and_xmp_without_changing_text_or_input(
        tmp_path, without_exiftool, capsys):
    from doc_sentinel import cli

    _require_real_wmr()
    pikepdf = pytest.importorskip("pikepdf")
    src = _pdf_with_metadata(tmp_path)
    dest = tmp_path / "cleaned.pdf"
    original_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    original_text = _pdf_text(src)
    assert b"ChatGPT" in src.read_bytes() and b"Microsoft 365 Copilot" in src.read_bytes()
    code = cli.main(["clean", str(src), "-o", str(dest), "--json"])
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert (code, report["verdict"]) == (0, "cleaned")
    assert captured.err == (f"doc-sentinel clean: verdict=cleaned\nfindings: none\n"
                            f"output: {dest}\n")
    assert not report["adapters_failed"]
    assert report["c2pa"] == {"before": False, "after": False, "actions": []}
    for key in ("/Producer", "/Creator"):
        assert {"adapter": "watermarks-remover", "action": "removed",
                "what": f"PDF Info {key} (pikepdf fallback)", "count": 1} in report["clean_actions"]
    with pikepdf.open(dest) as pdf:
        assert "/Producer" not in pdf.docinfo and "/Creator" not in pdf.docinfo
        assert "/Metadata" not in pdf.Root or not pdf.Root.Metadata.read_bytes().strip()
    assert b"ChatGPT" not in dest.read_bytes() and b"Microsoft 365 Copilot" not in dest.read_bytes()
    assert _pdf_text(dest) == original_text
    assert hashlib.sha256(src.read_bytes()).hexdigest() == original_sha


@pytest.mark.integration
def test_pdf_fallback_preserves_non_ai_producer(tmp_path, without_exiftool, capsys):
    from doc_sentinel import cli

    _require_real_wmr()
    pikepdf = pytest.importorskip("pikepdf")
    src = _pdf_with_metadata(tmp_path, producer="LibreOffice 7.6", creator="Ordinary editor")
    dest = tmp_path / "cleaned.pdf"
    original = hashlib.sha256(src.read_bytes()).hexdigest()
    code = cli.main(["clean", str(src), "-o", str(dest), "--json"])
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert (code, report["verdict"]) == (0, "cleaned")
    assert captured.err == (f"doc-sentinel clean: verdict=cleaned\nfindings: none\n"
                            f"output: {dest}\n")
    with pikepdf.open(dest) as pdf:
        assert str(pdf.docinfo["/Producer"]) == "LibreOffice 7.6"
        assert str(pdf.docinfo["/Creator"]) == "Ordinary editor"
        assert "/Metadata" not in pdf.Root or not pdf.Root.Metadata.read_bytes().strip()
    assert not any(action["what"].startswith("PDF Info /Producer")
                   for action in report["clean_actions"])
    assert _pdf_text(src) == _pdf_text(dest)
    assert hashlib.sha256(src.read_bytes()).hexdigest() == original


@pytest.mark.integration
def test_pdf_residue_is_independently_reported_when_fallback_cannot_remove_it(
        tmp_path, without_exiftool, monkeypatch, capsys):
    from doc_sentinel import cli

    _require_real_wmr()
    src = _pdf_with_metadata(tmp_path)
    dest = tmp_path / "cleaned.pdf"
    original = hashlib.sha256(src.read_bytes()).hexdigest()
    monkeypatch.setattr(_module(), "_pdf_fallback", lambda path, ctx: [], raising=False)
    code = cli.main(["clean", str(src), "-o", str(dest), "--json"])
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert (code, report["verdict"]) == (1, "residual")
    assert captured.err == (f"doc-sentinel clean: verdict=residual\nfindings: none\n"
                            f"output: {dest}\n")
    assert not report["adapters_failed"]
    for key in ("/Producer", "/Creator", "XMP"):
        assert any(action["action"] == "residual" and
                   action["what"].startswith(f"PDF metadata {key}") and action["count"] >= 1
                   for action in report["clean_actions"]), report["clean_actions"]
    assert report["c2pa"] == {"before": False, "after": False, "actions": []}
    assert _pdf_text(src) == _pdf_text(dest)
    assert hashlib.sha256(src.read_bytes()).hexdigest() == original


def test_no_c2pa_actions_without_preclean_c2pa(monkeypatch, tmp_path):
    mod = _module()
    src, dest = tmp_path / "in.docx", tmp_path / "out.docx"
    src.write_bytes(b"original")
    inspection = {"kind": "container", "has_c2pa": False, "has_ai_metadata": False,
                  "layer_a_hits": []}

    def fake_run(cmd, ctx, **kwargs):
        if str(cmd[1]).endswith("/inspect_file.py"):
            return SimpleNamespace(stdout=json.dumps(inspection), stderr="", returncode=0)
        dest.write_bytes(b"cleaned")
        return SimpleNamespace(stdout=json.dumps({"kind": "container", "actions": [
            'deep image pass not needed for AI/C2PA markers',
            'c2patool available for inspect; strip via exiftool/re-export',
        ]}), stderr="", returncode=0)

    monkeypatch.setattr(mod, "run_tool", fake_run)
    result = _adapter().clean(src, dest, _ctx(tmp_path))
    assert not result.c2pa_before and not result.c2pa_after and not result.residual
    assert not any(action.get("c2pa") or "c2pa" in action["what"].lower()
                   for action in result.actions)
    assert src.read_bytes() == b"original"


@pytest.mark.parametrize("c2_after", [False, True])
def test_false_c2pa_before_never_marks_an_embedded_action_as_c2pa(
        monkeypatch, tmp_path, c2_after):
    mod = _module()
    src, dest = tmp_path / "in.docx", tmp_path / "out.docx"
    src.write_bytes(b"original")
    before = {"kind": "container", "has_c2pa": False, "has_ai_metadata": False,
              "layer_a_hits": [], "findings": ["word/media/image1.png: c2pa probe"]}
    after = {"kind": "container", "has_c2pa": c2_after, "has_ai_metadata": False,
             "layer_a_hits": []}

    def fake_run(cmd, ctx, **kwargs):
        if str(cmd[1]).endswith("/inspect_file.py"):
            return SimpleNamespace(stdout=json.dumps(before if cmd[-1] == str(src) else after),
                                   stderr="", returncode=0)
        dest.write_bytes(b"cleaned")
        return SimpleNamespace(stdout=json.dumps({"kind": "container", "actions": [
            "clean embedded media in word/media/image1.png (drop PNG tEXt)"]}),
            stderr="", returncode=0)

    monkeypatch.setattr(mod, "run_tool", fake_run)
    result = _adapter().clean(src, dest, _ctx(tmp_path))
    assert result.c2pa_before is False and result.c2pa_after is c2_after
    assert result.residual is c2_after
    assert not any(action.get("c2pa") for action in result.actions)
    assert src.read_bytes() == b"original"


def test_c2pa_informational_messages_are_not_removal_actions(monkeypatch, tmp_path):
    mod = _module()
    src, dest = tmp_path / "in.docx", tmp_path / "out.docx"
    src.write_bytes(b"original")

    def fake_run(cmd, ctx, **kwargs):
        if str(cmd[1]).endswith("/inspect_file.py"):
            return SimpleNamespace(stdout=json.dumps({
                "kind": "container", "has_c2pa": cmd[-1] == str(src),
                "has_ai_metadata": False, "layer_a_hits": [],
            }), stderr="", returncode=0)
        dest.write_bytes(b"cleaned")
        return SimpleNamespace(stdout=json.dumps({"kind": "container", "actions": [
            "deep image pass not needed for AI/C2PA markers",
            "residual AI/C2PA markers after document-level strip; running deep image pass",
            "c2patool available for inspect; strip via exiftool/re-export",
        ]}), stderr="", returncode=0)

    monkeypatch.setattr(mod, "run_tool", fake_run)
    result = _adapter().clean(src, dest, _ctx(tmp_path))
    assert result.c2pa_before and not result.c2pa_after and not result.residual
    assert result.actions == [{"action": "removed", "what": "C2PA detected before cleaning",
                               "count": 1, "c2pa": True}]
    assert src.read_bytes() == b"original"


@pytest.mark.parametrize("marker", [
    "ChatGPT", "OpenAI", "GPT-4", "GPT-4o", "GPT-5", "Copilot", "Gemini", "Bard",
    "Claude", "Anthropic", "Midjourney", "DALL-E", "Stable Diffusion", "Firefly",
    "Perplexity", "Llama", "Mistral", "Grok", "Generated by AI",
])
@pytest.mark.integration
def test_pdf_ai_info_vocabulary_is_independently_verified(
        tmp_path, without_exiftool, monkeypatch, marker):
    adapter = _require_real_wmr()
    src = _pdf_with_metadata(tmp_path, producer=marker, creator="Ordinary editor", xmp=False)
    dest = tmp_path / "cleaned.pdf"
    original = hashlib.sha256(src.read_bytes()).hexdigest()
    monkeypatch.setattr(_module(), "_pdf_fallback", lambda path, ctx: [])
    result = adapter.clean(src, dest, _ctx(tmp_path))
    assert result.residual
    assert {"action": "residual", "what": "PDF metadata /Producer", "count": 1} in result.actions
    assert _pdf_text(src) == _pdf_text(dest)
    assert hashlib.sha256(src.read_bytes()).hexdigest() == original


@pytest.mark.integration
def test_pdf_verification_is_also_independent_when_exiftool_is_available(
        tmp_path, without_exiftool, monkeypatch):
    import shutil

    adapter = _require_real_wmr()
    src = _pdf_with_metadata(tmp_path)
    dest = tmp_path / "cleaned.pdf"
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name: "/simulated/exiftool" if name == "exiftool"
                        else real_which(name))
    result = adapter.clean(src, dest, _ctx(tmp_path))
    assert result.residual
    assert {"action": "residual", "what": "PDF metadata /Producer", "count": 1} in result.actions
    assert any(action["what"].startswith("PDF metadata XMP ") for action in result.actions)
    assert _pdf_text(src) == _pdf_text(dest)


def test_pdf_clean_refuses_if_upstream_changes_page_text(tmp_path, without_exiftool, monkeypatch):
    canvas = pytest.importorskip("reportlab.pdfgen.canvas")
    src = _pdf_with_metadata(tmp_path)
    original = hashlib.sha256(src.read_bytes()).hexdigest()
    changed = tmp_path / "changed.pdf"
    document = canvas.Canvas(str(changed))
    document.drawString(72, 720, "A different page")
    document.showPage()
    document.save()
    dest = tmp_path / "cleaned.pdf"
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_inspect", lambda path, ctx: {
        "kind": "container", "format": "pdf", "has_c2pa": False,
        "has_ai_metadata": False, "layer_a_hits": [],
    })

    def changed_output(cmd, ctx, **kwargs):
        dest.write_bytes(changed.read_bytes())
        return SimpleNamespace(stdout=json.dumps({"kind": "container", "actions": []}),
                               stderr="", returncode=0)

    monkeypatch.setattr(_module(), "run_tool", changed_output)
    with pytest.raises(AdapterError, match="page text"):
        adapter.clean(src, dest, _ctx(tmp_path))
    assert hashlib.sha256(src.read_bytes()).hexdigest() == original


@pytest.mark.integration
def test_pdf_fallback_refuses_output_if_rewrite_changes_page_text(
        tmp_path, without_exiftool, monkeypatch):
    import pymupdf

    adapter = _require_real_wmr()
    src = _pdf_with_metadata(tmp_path)
    dest = tmp_path / "cleaned.pdf"
    original_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    real_open = pymupdf.open

    class ChangedDocument:
        def __enter__(self):
            return [SimpleNamespace(get_text=lambda: "changed text")]

        def __exit__(self, *args):
            return False

    def open_with_changed_rewrite(path):
        if Path(path).name == "rewritten.pdf":
            return ChangedDocument()
        return real_open(path)

    monkeypatch.setattr(pymupdf, "open", open_with_changed_rewrite)
    with pytest.raises(AdapterError, match="PDF metadata rewrite changed page text"):
        adapter.clean(src, dest, _ctx(tmp_path))
    assert hashlib.sha256(src.read_bytes()).hexdigest() == original_sha
    assert _pdf_text(src) == _pdf_text(dest)
