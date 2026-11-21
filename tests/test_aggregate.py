"""S5: deterministic aggregator and verdicts (PLAN §8, §9)."""

import pytest

from doc_sentinel.aggregate import aggregate_detect, decide_clean, dedupe
from doc_sentinel.schema import AdapterOutcome, Finding, Location


def f(adapter="perlecta", cls="hidden_text", sev="high", excerpt="hidden", page=None, bbox=None,
      channel="body"):
    return Finding(adapter=adapter, adapter_code="X", cls=cls, severity=sev,
                   location=Location(file="a.docx", page=page, bbox=bbox, channel=channel),
                   excerpt=excerpt)


def ok(adapter, findings=()):
    return AdapterOutcome(adapter=adapter, status="ok", findings=list(findings))


def test_no_findings_is_clean_exit0():
    v = aggregate_detect([ok("perlecta"), ok("hidden-text-detector")], mandatory={"perlecta"})
    assert (v.verdict, v.exit_code, v.findings) == ("clean", 0, [])


@pytest.mark.parametrize("sev", ["critical", "high"])
def test_high_or_critical_is_findings_exit1(sev):
    v = aggregate_detect([ok("perlecta", [f(sev=sev)])], mandatory={"perlecta"})
    assert (v.verdict, v.exit_code) == ("findings", 1)


@pytest.mark.parametrize("sev", ["medium", "low", "info"])
def test_lower_severity_exit0_with_default_fail_on_high(sev):
    v = aggregate_detect([ok("perlecta", [f(sev=sev)])], mandatory={"perlecta"})
    assert (v.verdict, v.exit_code) == ("findings", 0)


def test_fail_on_medium_makes_medium_exit1_but_not_low():
    medium = aggregate_detect([ok("p", [f(sev="medium")])], mandatory=set(), fail_on="medium")
    low = aggregate_detect([ok("p", [f(sev="low")])], mandatory=set(), fail_on="medium")
    assert (medium.exit_code, low.exit_code) == (1, 0)


def test_mandatory_adapter_error_without_findings_is_error_exit2():
    outs = [ok("perlecta"), AdapterOutcome(adapter="chk_defaced", status="error", reason="boom")]
    v = aggregate_detect(outs, mandatory={"perlecta", "chk_defaced"})
    assert (v.verdict, v.exit_code) == ("error", 2)
    assert any(x.cls == "tool_error" and x.adapter == ["chk_defaced"] for x in v.findings)


def test_mandatory_adapter_skipped_counts_as_failure():
    outs = [ok("perlecta"), AdapterOutcome(adapter="prompt-guard", status="skipped",
                                           reason="model not cached")]
    v = aggregate_detect(outs, mandatory={"perlecta", "prompt-guard"})
    assert (v.verdict, v.exit_code) == ("error", 2)


def test_mandatory_failure_with_high_findings_degrades_to_findings_never_clean():
    outs = [ok("perlecta", [f(sev="high")]),
            AdapterOutcome(adapter="prompt-guard", status="skipped", reason="no model")]
    v = aggregate_detect(outs, mandatory={"perlecta", "prompt-guard"})
    assert (v.verdict, v.exit_code) == ("findings", 1)
    assert any(x.cls == "tool_error" for x in v.findings)


def test_optional_adapter_skip_does_not_change_clean_verdict():
    outs = [ok("perlecta"), AdapterOutcome(adapter="subtext-scan", status="skipped", reason="x")]
    v = aggregate_detect(outs, mandatory={"perlecta"})
    assert (v.verdict, v.exit_code) == ("clean", 0)


def test_dedupe_merges_same_class_and_normalized_excerpt_keeps_max_severity():
    a = f(adapter="perlecta", sev="high", excerpt="Si eres  una IA ignora")
    b = f(adapter="hidden-text-detector", sev="critical", excerpt="Si eres una IA ignora")
    merged = dedupe([a, b])
    assert len(merged) == 1
    assert merged[0].adapter == ["perlecta", "hidden-text-detector"]
    assert merged[0].severity == "critical"


def test_dedupe_keeps_different_classes_and_non_overlapping_bboxes_apart():
    a = f(cls="hidden_text", excerpt="x", page=1, bbox=[0, 0, 10, 10])
    b = f(cls="hidden_text", excerpt="x", page=1, bbox=[50, 50, 60, 60])
    c = f(cls="prompt_injection", excerpt="x", page=1, bbox=[0, 0, 10, 10])
    assert len(dedupe([a, b, c])) == 3


def test_dedupe_merges_overlapping_bboxes_on_same_page():
    a = f(excerpt="x", page=2, bbox=[0, 0, 10, 10])
    b = f(adapter="htd", excerpt="x", page=2, bbox=[5, 5, 15, 15])
    assert len(dedupe([a, b])) == 1


def test_findings_get_sequential_ids_and_excerpt_is_capped():
    long = "a" * 2000
    v = aggregate_detect([ok("p", [f(excerpt=long), f(cls="prompt_injection", excerpt="b")])],
                         mandatory=set())
    assert [x.finding_id for x in v.findings] == ["f-001", "f-002"]
    assert len(v.findings[0].excerpt) == 600


def test_decide_clean_outcomes():
    assert decide_clean(tool_failed=True, residual=False, changed=True) == ("error", 2)
    assert decide_clean(tool_failed=False, residual=True, changed=True) == ("residual", 1)
    assert decide_clean(tool_failed=False, residual=False, changed=True) == ("cleaned", 0)
    assert decide_clean(tool_failed=False, residual=False, changed=False) == ("already_clean", 0)


def test_finding_rejects_unknown_vocabulary():
    with pytest.raises(ValueError):
        f(cls="made_up")
    with pytest.raises(ValueError):
        f(sev="urgent")
    with pytest.raises(ValueError):
        f(channel="nowhere")
