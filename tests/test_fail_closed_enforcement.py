"""
Enforcement tests — fail-closed for RETURNED decision results.

test_decision_result_conformance.py proves normalize_decision() classifies an
out-of-contract decision result correctly. These tests prove the RUNTIME acts on
that classification: for every malformed or unrecognized result, the tool path
and the LLM cost path must refuse the call with a ShackleInterrupt, never
execute it, and never commit spend.

Regression target (pre-patch behaviour on this same corpus):
  * unknown/near-miss verdicts -> tool EXECUTED and spend COMMITTED (fail open)
  * non-pair returns           -> TypeError/ValueError raised outside every
                                  handler, so no audit record and no HITL

Usage:
    pytest tests/test_fail_closed_enforcement.py -v
"""

import pytest

import shackle.core as core
from shackle.core import ExecutionState, ShackleInterrupt, TriggerEngine


class LyingStr(str):
    """A str subclass whose equality operator returns True for anything.

    Models a hostile or broken producer: `verdict == "ALLOW"` is True while the
    value is not the enum member. Defeated only by an exact-type check.

    Defined locally rather than imported from the sibling test module: there is
    no tests/__init__.py, so a cross-test import resolves only when the repo
    root happens to be on sys.path. Not worth staking CI on.
    """

    def __eq__(self, other):
        return True

    def __ne__(self, other):
        return False

    __hash__ = str.__hash__


def _gen():
    yield "ALLOW"
    yield "within_thresholds"


# Every out-of-contract decision result. None of these may release a call.
BAD_RESULTS = [
    ("missing", None),
    ("empty_tuple", ()),
    ("one_tuple", ("DENY",)),
    ("three_tuple", ("ALLOW", "within_thresholds", "extra")),
    ("bare_string_two_chars", "OK"),
    ("bare_string_allow", "ALLOW"),
    ("bytes_allow", b"ALLOW"),
    ("mapping", {"verdict": "ALLOW", "reason": "within_thresholds"}),
    ("unordered_set", {"ALLOW", "within_thresholds"}),
    ("unknown_verdict", ("PERMIT", "looks_fine")),
    ("lowercase_allow", ("allow", "within_thresholds")),
    ("padded_allow", (" ALLOW ", "within_thresholds")),
    ("empty_verdict", ("", "")),
    ("lowercase_hitl", ("hitl", "needs_review")),
    ("verdict_true", (True, "within_thresholds")),
    ("verdict_int", (1, "ok")),
    ("verdict_none", (None, None)),
    ("allow_reason_none", ("ALLOW", None)),
    ("allow_reason_blank", ("ALLOW", "   ")),
    ("allow_reason_int", ("ALLOW", 7)),
    ("allow_reason_control_chars", ("ALLOW", "ok\ninjected")),
    ("lying_str_verdict", (LyingStr("ALLOW"), "within_thresholds")),
    ("generator", _gen()),
    ("bare_object", object()),
]

IDS = [name for name, _ in BAD_RESULTS]


@pytest.fixture
def patched_decide(monkeypatch):
    """Replace the decision source with one that RETURNS a given result."""
    def install(result):
        monkeypatch.setattr(core, "_sp_decide", lambda *a, **k: result)
    return install


# ── tool path ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("result", [r for _, r in BAD_RESULTS], ids=IDS)
def test_tool_path_denies_malformed_result(patched_decide, result):
    patched_decide(result)
    engine = TriggerEngine(budget=10.0, max_repeat_calls=3)
    state = ExecutionState()
    with pytest.raises(ShackleInterrupt) as ei:
        engine.evaluate_tool_call("Agent", "send_email", {"to": "x@y.z"}, state)
    si = ei.value
    assert si.trigger_type == "MALFORMED_DECISION", si.trigger_type
    assert state.last_decision[0] == "DENY"


@pytest.mark.parametrize("result", [r for _, r in BAD_RESULTS], ids=IDS)
def test_tool_path_records_no_nonce_on_denial(patched_decide, result):
    """A denied call must not consume its anti-replay nonce."""
    patched_decide(result)
    engine = TriggerEngine(budget=10.0)
    state = ExecutionState()
    with pytest.raises(ShackleInterrupt):
        engine.evaluate_tool_call("Agent", "wire_transfer", {"amt": 1}, state,
                                  nonce="n-1")
    assert state.seen_nonces == []


@pytest.mark.parametrize("result", [r for _, r in BAD_RESULTS], ids=IDS)
def test_tool_path_raises_shackle_interrupt_not_type_error(patched_decide, result):
    """The failure must arrive as ShackleInterrupt.

    A bare TypeError/ValueError from tuple-unpacking is NOT equivalent: the
    framework patch sites catch only ShackleInterrupt, so anything else escapes
    without an audit record and without the operator HITL prompt.
    """
    patched_decide(result)
    engine = TriggerEngine(budget=10.0)
    state = ExecutionState()
    try:
        engine.evaluate_tool_call("Agent", "t", {"a": 1}, state)
    except ShackleInterrupt:
        pass
    except BaseException as exc:  # noqa: BLE001
        pytest.fail(f"escaped as {type(exc).__name__}: {exc}")
    else:
        pytest.fail("call was ALLOWED (fail-open)")


# ── LLM cost path ────────────────────────────────────────────────────────

@pytest.mark.parametrize("result", [r for _, r in BAD_RESULTS], ids=IDS)
def test_llm_path_denies_malformed_result(patched_decide, result):
    patched_decide(result)
    engine = TriggerEngine(budget=10.0)
    state = ExecutionState()
    with pytest.raises(ShackleInterrupt):
        engine.evaluate_llm_call("gpt-4o", 1000, 500, state)


@pytest.mark.parametrize("result", [r for _, r in BAD_RESULTS], ids=IDS)
def test_llm_path_commits_no_spend_on_denial(patched_decide, result):
    """A denied call never happened, so no cost or tokens may be recorded."""
    patched_decide(result)
    engine = TriggerEngine(budget=10.0)
    state = ExecutionState()
    with pytest.raises(ShackleInterrupt):
        engine.evaluate_llm_call("gpt-4o", 1_000_000, 1_000_000, state)
    assert state.total_cost == 0.0
    assert state.input_tokens == 0
    assert state.output_tokens == 0


# ── the raise case (already proven) must still hold ──────────────────────

def test_tool_path_denies_when_decide_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(core, "_sp_decide", boom)
    engine = TriggerEngine(budget=10.0)
    state = ExecutionState()
    with pytest.raises(ShackleInterrupt) as ei:
        engine.evaluate_tool_call("Agent", "t", {"a": 1}, state)
    assert state.last_decision == ("DENY", "decide_unavailable_fail_closed")
    assert ei.value.trigger_type == "MALFORMED_DECISION"


def test_tool_path_denies_when_decide_raises_base_exception(monkeypatch):
    def boom(*a, **k):
        raise KeyboardInterrupt()

    monkeypatch.setattr(core, "_sp_decide", boom)
    engine = TriggerEngine(budget=10.0)
    state = ExecutionState()
    with pytest.raises(ShackleInterrupt):
        engine.evaluate_tool_call("Agent", "t", {"a": 1}, state)


# ── in-contract results still behave exactly as before ───────────────────

def test_valid_allow_still_releases(patched_decide):
    patched_decide(("ALLOW", "within_thresholds"))
    engine = TriggerEngine(budget=10.0)
    state = ExecutionState()
    engine.evaluate_tool_call("Agent", "search", {"q": "x"}, state, nonce="n-1")
    assert state.last_decision == ("ALLOW", "within_thresholds")
    assert state.seen_nonces == ["n-1"]


def test_valid_allow_as_list_still_releases(patched_decide):
    patched_decide(["ALLOW", "within_thresholds"])
    engine = TriggerEngine(budget=10.0)
    state = ExecutionState()
    engine.evaluate_tool_call("Agent", "search", {"q": "x"}, state)
    assert state.last_decision == ("ALLOW", "within_thresholds")


def test_valid_deny_keeps_its_label(patched_decide):
    patched_decide(("DENY", "max_repeat_exceeded"))
    engine = TriggerEngine(budget=10.0)
    state = ExecutionState()
    with pytest.raises(ShackleInterrupt) as ei:
        engine.evaluate_tool_call("Agent", "search", {"q": "x"}, state)
    assert ei.value.trigger_type == "REPETITIVE_TOOL_CALL"


def test_valid_hitl_still_escalates(patched_decide):
    patched_decide(("HITL", "fail_closed:opaque_context"))
    engine = TriggerEngine(budget=10.0)
    state = ExecutionState()
    with pytest.raises(ShackleInterrupt) as ei:
        engine.evaluate_tool_call("Agent", "search", {"q": "x"}, state)
    assert ei.value.trigger_type == "HITL_REQUIRED"


def test_deny_with_unusable_reason_still_denies(patched_decide):
    """The verdict survives a broken reason; only the label is repaired."""
    patched_decide(("DENY", None))
    engine = TriggerEngine(budget=10.0)
    state = ExecutionState()
    with pytest.raises(ShackleInterrupt):
        engine.evaluate_tool_call("Agent", "search", {"q": "x"}, state)
    assert state.last_decision == (
        "DENY", "malformed_decision:unspecified_reason")


def test_hitl_with_unusable_reason_still_escalates(patched_decide):
    patched_decide(("HITL", None))
    engine = TriggerEngine(budget=10.0)
    state = ExecutionState()
    with pytest.raises(ShackleInterrupt) as ei:
        engine.evaluate_tool_call("Agent", "search", {"q": "x"}, state)
    assert ei.value.trigger_type == "HITL_REQUIRED"
    assert state.last_decision[0] == "HITL"
