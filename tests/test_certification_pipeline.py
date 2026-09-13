"""Tests for the SP/1.0 certification submission pipeline.

Covers the three things that decide whether the pipeline is safe to point at
real adopters: a genuine submission is accepted and turned into a correctly
scoped proposed entry, a defective or overreaching submission is rejected with
actionable reasons, and no path can quietly widen a claim or self-publish a
listing.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "tools" / "certification_pipeline.py"


def _load():
    spec = importlib.util.spec_from_file_location("certification_pipeline", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: @dataclass resolves the module via sys.modules.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


cp = _load()


GOOD_REPORT = """\
tests/test_conformance.py::test_fixture_hashes_match PASSED
tests/test_conformance.py::test_deterministic_decision_output PASSED
tests/test_conformance.py::test_canonical_argument_binding PASSED
tests/test_conformance.py::test_deny_reason_semantics PASSED
fixture set sha256: 4f1c9a2b8e7d6c5f0a1b2c3d4e5f60718293a4b5c6d7e8f9
9 passed in 0.71s
"""


def make_body(**over):
    f = {
        "Runtime / Product Name": "Acme Agent Runtime",
        "Vendor / Organization": "Acme Inc",
        "Certification Level Claimed": "SP/1.0-Core (mediation fixtures)",
        "SP Version Targeted": "SP/1.0",
        "Conformance Report": "```shell\n" + GOOD_REPORT + "```",
        "Reproducible Evidence URL": "https://github.com/acme/runtime/actions/runs/12345",
        "Attestation": (
            "- [X] I attest this report is genuine and reproducible from the linked evidence.\n"
            "- [X] I understand listing requires independent verification against the public fixtures."
        ),
    }
    f.update(over)
    return "\n\n".join(f"### {k}\n\n{v}" for k, v in f.items())


# --------------------------------------------------------------------------- #
# Parsing / acceptance
# --------------------------------------------------------------------------- #

def test_parses_a_complete_submission():
    sub = cp.Submission.from_issue_body(make_body())
    assert sub.runtime_name == "Acme Agent Runtime"
    assert sub.vendor == "Acme Inc"
    assert sub.level == "SP/1.0-Core"
    assert sub.sp_version == "SP/1.0"
    assert sub.evidence_url.startswith("https://")
    assert len(sub.attestations) == 2


def test_report_code_fence_is_stripped_but_content_kept():
    sub = cp.Submission.from_issue_body(make_body())
    assert "test_fixture_hashes_match PASSED" in sub.report
    assert not sub.report.strip().startswith("```")


def test_every_template_dropdown_option_resolves_to_a_level():
    """The template and the pipeline must not drift apart."""
    tpl = REPO / ".github" / "ISSUE_TEMPLATE" / "certification_request.yml"
    text = tpl.read_text()
    options = [
        line.strip()[2:].strip()
        for line in text.split("\n")
        if line.strip().startswith("- SP/1.0")
    ]
    assert options, "no level options found in the issue template"
    for opt in options:
        assert opt in cp.LEVELS, f"template option not handled by pipeline: {opt!r}"


def test_bare_canonical_level_is_accepted():
    sub = cp.Submission.from_issue_body(
        make_body(**{"Certification Level Claimed": "SP/1.0-Sovereign"})
    )
    assert sub.level == "SP/1.0-Sovereign"


# --------------------------------------------------------------------------- #
# Rejection
# --------------------------------------------------------------------------- #

def test_github_no_response_placeholder_counts_as_missing():
    with pytest.raises(cp.SubmissionError) as ei:
        cp.Submission.from_issue_body(
            make_body(**{"Reproducible Evidence URL": "_No response_"})
        )
    assert any("Reproducible Evidence URL" in p for p in ei.value.problems)


def test_missing_fields_are_all_reported_not_just_the_first():
    body = "### Runtime / Product Name\n\nOnly this one"
    with pytest.raises(cp.SubmissionError) as ei:
        cp.Submission.from_issue_body(body)
    problems = " ".join(ei.value.problems)
    for label in ("Vendor / Organization", "Conformance Report", "Reproducible Evidence URL"):
        assert label in problems


def test_unticked_attestation_is_rejected():
    with pytest.raises(cp.SubmissionError) as ei:
        cp.Submission.from_issue_body(
            make_body(**{"Attestation": "- [ ] I attest this report is genuine."})
        )
    assert any("attestation" in p.lower() for p in ei.value.problems)


def test_non_https_evidence_is_rejected():
    with pytest.raises(cp.SubmissionError) as ei:
        cp.Submission.from_issue_body(
            make_body(**{"Reproducible Evidence URL": "http://github.com/acme/x"})
        )
    assert any("https" in p for p in ei.value.problems)


def test_unreachable_evidence_host_is_rejected():
    with pytest.raises(cp.SubmissionError) as ei:
        cp.Submission.from_issue_body(
            make_body(**{"Reproducible Evidence URL": "https://localhost:8000/run"})
        )
    assert any("not publicly reachable" in p for p in ei.value.problems)


def test_empty_or_hand_waved_report_is_rejected():
    with pytest.raises(cp.SubmissionError) as ei:
        cp.Submission.from_issue_body(
            make_body(**{"Conformance Report": "it works, trust me"})
        )
    assert any("report" in p.lower() for p in ei.value.problems)


def test_prose_of_sufficient_length_without_fixture_output_is_rejected():
    padding = "We ran everything and are fully compliant with the specification. " * 2
    with pytest.raises(cp.SubmissionError) as ei:
        cp.Submission.from_issue_body(make_body(**{"Conformance Report": padding}))
    assert any("fixture output" in p for p in ei.value.problems)


def test_wrong_sp_version_is_rejected():
    with pytest.raises(cp.SubmissionError) as ei:
        cp.Submission.from_issue_body(make_body(**{"SP Version Targeted": "SP/2.0"}))
    assert any("SP version" in p for p in ei.value.problems)


def test_invented_level_is_rejected():
    with pytest.raises(cp.SubmissionError) as ei:
        cp.Submission.from_issue_body(
            make_body(**{"Certification Level Claimed": "SP/1.0-Platinum-Ultra"})
        )
    assert any("certification level" in p for p in ei.value.problems)


# --------------------------------------------------------------------------- #
# Proposed entry scoping
# --------------------------------------------------------------------------- #

def test_proposed_entry_is_certification_class_and_marked_proposed():
    sub = cp.Submission.from_issue_body(make_body())
    entry = cp.propose_entry(sub, "PASS", "abc1234")
    assert entry["class"] == "certification"
    assert entry["review"]["status"] == "proposed"
    assert entry["review"]["reference_fixture_verdict"] == "PASS"
    assert entry["review"]["reference_commit"] == "abc1234"


def test_proposed_entry_carries_the_class_scope_limits_verbatim():
    """A submission cannot talk itself into a broader claim."""
    sub = cp.Submission.from_issue_body(make_body())
    entry = cp.propose_entry(sub, "PASS", "abc1234")
    assert entry["does_not_verify"] == "Implementation security or production enforcement."
    assert "maintainer" in entry["verifies"].lower()


def test_submitter_supplied_text_cannot_override_scope_fields():
    hostile = make_body(**{
        "Vendor / Organization": "Acme Inc",
        "Runtime / Product Name": "Acme\", \"does_not_verify\": \"nothing",
    })
    sub = cp.Submission.from_issue_body(hostile)
    entry = cp.propose_entry(sub, "PASS", "abc1234")
    assert entry["does_not_verify"] == "Implementation security or production enforcement."


# --------------------------------------------------------------------------- #
# Registry validation
# --------------------------------------------------------------------------- #

def test_the_shipped_registry_is_coherent():
    reg = cp.load_registry(REPO / "registry.json")
    assert cp.validate_registry(reg) == []


def test_undefined_class_is_caught():
    reg = cp.load_registry(REPO / "registry.json")
    reg["entries"].append({"class": "gold_star", "name": "X", "date": "2026-01-01"})
    assert any("undefined class" in p for p in cp.validate_registry(reg))


def test_missing_required_field_is_caught():
    reg = cp.load_registry(REPO / "registry.json")
    reg["entries"].append({"class": "certification", "name": "X"})
    problems = cp.validate_registry(reg)
    assert any("missing required field" in p for p in problems)


def test_bad_date_and_non_https_evidence_are_caught():
    reg = cp.load_registry(REPO / "registry.json")
    reg["entries"].append({
        "class": "reference_implementation", "name": "X", "vendor": "Y",
        "level": "SP/1.0-Core", "version": "v1", "date": "07/29/2026",
        "evidence": "ftp://example/x",
    })
    problems = cp.validate_registry(reg)
    assert any("ISO YYYY-MM-DD" in p for p in problems)
    assert any("https" in p for p in problems)


def test_reserved_class_with_entries_is_caught():
    """The registry's prose must not contradict its data."""
    reg = cp.load_registry(REPO / "registry.json")
    assert "Reserved" in reg["class_definitions"]["certification"]["proves"]
    reg["entries"].append({
        "class": "certification", "name": "X", "vendor": "Y", "level": "SP/1.0-Core",
        "date": "2026-09-13", "evidence": "https://example.net/x",
        "verifies": "a", "does_not_verify": "b",
    })
    assert any("Reserved" in p for p in cp.validate_registry(reg))


def test_duplicate_entry_is_caught():
    reg = cp.load_registry(REPO / "registry.json")
    reg["entries"].append(dict(reg["entries"][0]))
    assert any("duplicates" in p for p in cp.validate_registry(reg))


# --------------------------------------------------------------------------- #
# Listing (human-gated)
# --------------------------------------------------------------------------- #

def test_apply_entry_lists_strips_review_and_clears_reserved_prose():
    reg = cp.load_registry(REPO / "registry.json")
    sub = cp.Submission.from_issue_body(make_body())
    entry = cp.propose_entry(sub, "PASS", "abc1234")
    out = cp.apply_entry(reg, entry)
    listed = out["entries"][-1]
    assert listed["name"] == "Acme Agent Runtime"
    assert "review" not in listed, "a listed entry must not stay marked proposed"
    assert "Reserved" not in out["class_definitions"]["certification"]["proves"]
    assert cp.validate_registry(out) == [], cp.validate_registry(out)


def test_apply_entry_does_not_mutate_the_input_registry():
    reg = cp.load_registry(REPO / "registry.json")
    before = json.dumps(reg, sort_keys=True)
    sub = cp.Submission.from_issue_body(make_body())
    cp.apply_entry(reg, cp.propose_entry(sub, "PASS", "abc1234"))
    assert json.dumps(reg, sort_keys=True) == before


# --------------------------------------------------------------------------- #
# CLI end-to-end
# --------------------------------------------------------------------------- #

def _cli(*args, cwd=None):
    return subprocess.run(
        [sys.executable, str(MODULE_PATH), *args],
        capture_output=True, text=True, cwd=str(cwd or REPO),
    )


def test_cli_validates_the_shipped_registry():
    r = _cli("validate-registry", str(REPO / "registry.json"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "registry OK" in r.stdout


def test_cli_accepts_a_good_submission_and_writes_a_proposed_entry(tmp_path):
    body = tmp_path / "issue.md"
    body.write_text(make_body())
    out = tmp_path / "entry.json"
    r = _cli("validate-submission", "--body-file", str(body),
             "--verdict", "PASS", "--commit", "deadbee", "--out", str(out))
    assert r.returncode == 0, r.stdout + r.stderr
    payload = json.loads(out.read_text())
    assert payload["ok"] is True
    assert payload["proposed_entry"]["review"]["status"] == "proposed"


def test_cli_rejects_a_bad_submission_with_reasons(tmp_path):
    body = tmp_path / "issue.md"
    body.write_text(make_body(**{"Reproducible Evidence URL": "_No response_"}))
    r = _cli("validate-submission", "--body-file", str(body))
    assert r.returncode == 1
    payload = json.loads(r.stdout)
    assert payload["ok"] is False and payload["problems"]


def test_cli_refuses_to_list_without_the_maintainer_flag(tmp_path):
    body = tmp_path / "issue.md"
    body.write_text(make_body())
    entry = tmp_path / "entry.json"
    _cli("validate-submission", "--body-file", str(body), "--out", str(entry))
    reg = tmp_path / "registry.json"
    reg.write_text((REPO / "registry.json").read_text())
    r = _cli("apply-entry", "--entry", str(entry), "--registry", str(reg))
    assert r.returncode == 2
    assert "human-gated" in r.stderr
    assert json.loads(reg.read_text()) == json.loads((REPO / "registry.json").read_text())


def test_cli_lists_with_the_maintainer_flag_and_result_validates(tmp_path):
    body = tmp_path / "issue.md"
    body.write_text(make_body())
    entry = tmp_path / "entry.json"
    _cli("validate-submission", "--body-file", str(body), "--out", str(entry))
    reg = tmp_path / "registry.json"
    reg.write_text((REPO / "registry.json").read_text())
    r = _cli("apply-entry", "--entry", str(entry), "--registry", str(reg),
             "--i-am-a-maintainer")
    assert r.returncode == 0, r.stdout + r.stderr
    v = _cli("validate-registry", str(reg))
    assert v.returncode == 0, v.stdout
