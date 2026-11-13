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
