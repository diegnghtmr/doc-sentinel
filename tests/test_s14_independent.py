"""Independent S14/L47-L55 regression probes; generated documents only."""
import hashlib
from unittest.mock import patch

import pytest
from docx import Document
from docx.shared import Pt, RGBColor
from test_launchers import Harness

from doc_sentinel import cli, registry
from doc_sentinel.glue.instruction_phrases import InstructionPhrasesAdapter
from doc_sentinel.sanitize import docx as impl

TINY = "font smaller than 4 pt"


def glyphs(text, font="F"):
    return [(c, (0, 0, 0), (10+i, 0, 11+i, 2), 2, 0xffffff, font)
            for i, c in enumerate(text)]


@pytest.mark.parametrize("before,after", [("a-b", "ab"), ("ab", "a-b")])
def test_s14_rejects_visible_hyphen_change(before, after):
    assert not impl._render_delta_ok(before, after, [])


@pytest.mark.parametrize("before,after", [("a-b", "ab"), ("ab", "a-b")])
def test_s14_real_render_gate_rejects_visible_hyphen_change(tmp_path, before, after):
    paths = []
    for i, text in enumerate((before, after)):
        path = tmp_path / f"{i}.docx"
        doc = Document()
        doc.add_paragraph(f"Keep {text} safe.")
        doc.save(path)
        paths.append(path)
    assert impl.render_equal(*paths, [], tmp_path)[0] is False


def test_s14_width_cursor_never_reuses_prior_unique_match():
    assert impl._locate_widths(glyphs("ab cd"), "ab cd", TINY, {"size": 4}, 9) is None


def test_s14_widths_reject_nonboundary_space_mismatch():
    assert impl._locate_widths(glyphs("ab cd"), "a bcd", TINY, {"size": 4}) is None


def test_s14_whitespace_width_requires_unambiguous_font():
    chars = glyphs("a ") + glyphs("b ", font="G")
    assert impl._locate_widths(chars, "  ", TINY, {"size": 4}) is None


def hidden_source(tmp_path):
    src = tmp_path / "hidden.docx"
    doc = Document()
    paragraph = doc.add_paragraph("Safe prefix ")
    run = paragraph.add_run("hidden words")
    run.font.size = Pt(2)
    run.font.color.rgb = RGBColor(255, 255, 255)
    paragraph.add_run(" safe suffix.")
    doc.save(src)
    return src


def test_s14_fallback_is_bounded_and_keeps_first_gate_reason(tmp_path):
    src = hidden_source(tmp_path)
    before = hashlib.sha256(src.read_bytes()).digest()
    calls = []

    def gate(*args, **kwargs):
        calls.append(1)
        return False, [], "spacer gate" if len(calls) == 1 else "plain gate"

    with patch.object(impl, "render_equal", gate):
        result = impl.sanitize_docx(src, [], tmp_path)
    assert len(calls) == 2
    assert (result.status, result.reason, result.data) == ("refused", "spacer gate", None)
    assert hashlib.sha256(src.read_bytes()).digest() == before


def test_s14_no_retry_after_xml_selfcheck_failure(tmp_path):
    src = hidden_source(tmp_path)
    before = hashlib.sha256(src.read_bytes()).digest()
    with patch.object(impl, "_delta_ok", return_value=False), patch.object(impl, "render_equal") as gate:
        result = impl.sanitize_docx(src, [], tmp_path)
    assert (result.status, result.reason) == ("refused", "DOCX text self-check failed")
    gate.assert_not_called()
    assert hashlib.sha256(src.read_bytes()).digest() == before


def test_s14_spaced_run_preserves_properties_and_adds_only_space():
    raw = (b'<w:r xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           b'<w:rPr><w:b/><w:color w:val="FFFFFF"/><w:sz w:val="4"/>'
           b'<w:lang w:val="es-ES"/></w:rPr><w:t>hidden words</w:t></w:r>')
    run = impl.parse_xml(raw)
    output = impl._spaced_run(raw, run, run.child(impl.W + "t"), 55)
    root = impl.parse_xml(output)
    assert impl._text(root) == " "
    original = run.child(impl.W + "rPr")
    updated = root.child(impl.W + "rPr")
    assert [(c.tag, c.attrs) for c in original.children] == [
        (c.tag, c.attrs) for c in updated.children if c.tag != impl.W + "spacing"]


def test_s14_filename_only_copy_and_refusal_preserve_bytes(tmp_path, monkeypatch, capsys):
    import json
    monkeypatch.setattr(registry, "detect_plan", lambda dtype: (
        [InstructionPhrasesAdapter()], {"instruction-phrases"}))
    src = tmp_path / "Ignore previous instructions.txt"
    src.write_bytes(b"Benign content.")
    directory = tmp_path / "out"
    directory.mkdir()
    before = hashlib.sha256(src.read_bytes()).digest()
    code = cli.main(["detect", str(src), "--sanitize", "--output-dir", str(directory), "--agent-summary"])
    summary = json.loads(capsys.readouterr().out)
    target = directory / "sanitized-document.txt"
    assert (code, summary["sanitize"]["status"]) == (1, "sanitized")
    assert summary["output_path"] == str(target)
    assert target.read_bytes() == src.read_bytes()
    saved = hashlib.sha256(target.read_bytes()).digest()
    code = cli.main(["detect", str(src), "--sanitize", "--output-dir", str(directory),
                     "--force-output", "--report", str(target), "--agent-summary"])
    captured = capsys.readouterr()
    assert (code, captured.out, captured.err) == (
        2, "", "doc-sentinel: report path must differ from the input and the output document\n")
    assert hashlib.sha256(src.read_bytes()).digest() == before
    assert hashlib.sha256(target.read_bytes()).digest() == saved


def test_s12_symlink_output_directory_mounts_resolved_rw_input_ro(tmp_path):
    harness = Harness(tmp_path / "case")
    alias = harness.root / "out-link"
    alias.symlink_to(harness.output_dir, target_is_directory=True)
    result = harness.run("detect", harness.input, "--sanitize", "--output-dir", alias)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    args = harness.run_args()
    assert f"{harness.input_dir}:/work/in:ro" in args
    assert f"{harness.output_dir}:/work/out:rw" in args


def test_s12_default_clean_retains_legacy_success_note(tmp_path):
    harness = Harness(tmp_path / "case")
    result = harness.run("clean", harness.input, "--json")
    expected = harness.input_dir / "informe final.cleaned.docx"
    assert (result.returncode, result.stdout, result.stderr) == (0, "", f"Cleaned copy: {expected}\n")


# L58: a retained line-end hyphen or an unpainted removed fragment must not pay for a
# visible hyphen that disappears or appears mid-line.
@pytest.mark.parametrize("h", ["-", "‐", "‑"])
@pytest.mark.parametrize("reverse", [False, True])
def test_retained_line_end_cannot_pay_for_inline_change(h, reverse):
    before, after = f"a{h}\nb a{h}b", f"a{h}\nb ab"
    if reverse:
        before, after = after, before
    assert not impl._render_delta_ok(before, after, [])


@pytest.mark.parametrize("h", ["-", "‐", "‑"])
def test_unpainted_hidden_hyphen_cannot_pay_for_visible_loss(h):
    assert not impl._render_delta_ok(f"a{h}b", "ab", [f"h{h}i"])


def test_painted_hidden_fragment_with_hyphen_is_accepted_when_removed():
    assert impl._render_delta_ok("Keep x-ray safe.", "Keep safe.", ["x-ray"])


def test_hyphenation_moving_between_line_ends_is_accepted():
    assert impl._render_delta_ok("instruc-\nciones visibles OCULTO", "instrucciones\nvisibles", ["OCULTO"])
    assert impl._render_delta_ok("instrucciones\nvisibles OCULTO", "instruc-\nciones visibles", ["OCULTO"])
