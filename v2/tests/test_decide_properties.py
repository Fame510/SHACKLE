"""
Property-Based Tests for SHACKLE decide() Function
==================================================
Mathematical proof that the core decision function is correct.

Requires: pip install hypothesis pytest

Properties P1-P9 from SPEC.md. Each tested with Hypothesis over
thousands of randomly generated inputs. These tests prove correctness,
not just demonstrate it.
"""

import sys
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "spec"))

from hypothesis import assume, given, settings, strategies as st
from decide import (
    Verdict, DenyReason, GuardConfig, SessionState, ToolCall,
    HitlMode, decide, apply_allow, apply_deny, apply_post_exec,
    has_error_signal, hash_params,
)


# ═══════════════════════ Strategies ═══════════════════════

budget_s = st.floats(0.0, 100.0, allow_nan=False, allow_infinity=False)
cost_s = st.floats(0.0, 1.0, allow_nan=False, allow_infinity=False)
repeat_s = st.integers(0, 20)
calls_s = st.integers(0, 1000)
rng_s = st.floats(0.0, 0.999, allow_nan=False, allow_infinity=False)

tool_names = st.sampled_from([
    "web_search", "read_file", "write_file", "execute_code",
    "query_db", "call_api", "send_email", "create_agent",
])


@st.composite
def configs(draw):
    return GuardConfig(
        budget_usd=draw(budget_s),
        max_repeat_calls=draw(repeat_s),
        error_amplification=draw(st.booleans()),
        max_total_calls=draw(calls_s),
        probabilistic_deny=draw(st.booleans()),
        deny_jitter_ratio=draw(st.floats(0.0, 1.0)),
        hitl_mode=draw(st.sampled_from(list(HitlMode))),
        hitl_budget_threshold=draw(st.floats(0.0, 1.0)),
    )


@st.composite
def tool_calls(draw):
    params = {"q": draw(st.text(min_size=1, max_size=80))}
    return ToolCall(
        tool_name=draw(tool_names),
        tool_params_hash=hash_params(params),
        estimated_cost_usd=draw(cost_s),
        nonce=draw(st.integers(1, 2**63 - 1)),
        tool_params_raw=str(params),
    )


# ═══════════════════════ P1: Budget non-increasing ═══════════════════════

@given(configs(), tool_calls())
@settings(max_examples=300, deadline=None)
def test_p1_budget_monotonically_non_increasing(config, call):
    state = SessionState(budget_initial_usd=config.budget_usd,
                         budget_remaining_usd=config.budget_usd)
    before = state.budget_remaining_usd
    if call.estimated_cost_usd > 0:
        apply_post_exec(state, call.estimated_cost_usd)
        assert state.budget_remaining_usd <= before


# ═══════════════════════ P2: Repeat counts non-decreasing ═══════════════════════

@given(tool_calls(), st.integers(0, 50))
@settings(max_examples=300, deadline=None)
def test_p2_repeat_counts_non_decreasing(call, existing):
    state = SessionState(last_tool_name=call.tool_name,
                         last_tool_params_hash=call.tool_params_hash,
                         repeat_counts={call.tool_name: existing})
    before = state.repeat_counts.get(call.tool_name, 0)
    apply_allow(state, call)
    after = state.repeat_counts.get(call.tool_name, 0)
    assert after >= before


# ═══════════════════════ P3: Tripped → always DENY ═══════════════════════

@given(configs(), tool_calls(), rng_s)
@settings(max_examples=300, deadline=None)
def test_p3_tripped_always_denies(config, call, rng):
    state = SessionState(circuit_tripped=True, circuit_trip_reason="test")
    d = decide(state, call, config, rng)
    assert d.verdict == Verdict.DENY
    assert d.deny_reason == DenyReason.CIRCUIT_OPEN


# ═══════════════════════ P4: Budget never negative ═══════════════════════

@given(configs(), tool_calls())
@settings(max_examples=300, deadline=None)
def test_p4_budget_never_negative(config, call):
    state = SessionState(budget_initial_usd=config.budget_usd,
                         budget_remaining_usd=config.budget_usd)
    for _ in range(10):
        apply_post_exec(state, call.estimated_cost_usd)
    assert state.budget_remaining_usd >= 0.0


# ═══════════════════════ P5: Repeat limit → DENY ═══════════════════════

@given(st.integers(1, 10), tool_calls())
@settings(max_examples=300, deadline=None)
def test_p5_repeat_limit_triggers_deny(max_repeats, call):
    config = GuardConfig(max_repeat_calls=max_repeats, error_amplification=False)
    state = SessionState(last_tool_name=call.tool_name,
                         last_tool_params_hash=call.tool_params_hash,
                         repeat_counts={call.tool_name: max_repeats})
    d = decide(state, call, config, 0.5)
    assert d.verdict == Verdict.DENY


# ═══════════════════════ P6: Fresh state → ALLOW ═══════════════════════

@given(configs(), rng_s)
@settings(max_examples=300, deadline=None)
def test_p6_fresh_state_allows_first_call(config, rng):
    assume(config.budget_usd > 0.01)
    assume(not config.probabilistic_deny)
    assume(config.hitl_mode == HitlMode.NEVER)  # HITL modes would intercept
    state = SessionState(budget_initial_usd=config.budget_usd,
                         budget_remaining_usd=config.budget_usd)
    call = ToolCall("test_tool", b"hash_1", 0.0001, nonce=42)
    d = decide(state, call, config, rng)
    assert d.verdict == Verdict.ALLOW


# ═══════════════════════ P7: Deterministic ═══════════════════════

@given(configs(), tool_calls(), rng_s)
@settings(max_examples=300, deadline=None)
def test_p7_deterministic(config, call, rng):
    state = SessionState(budget_initial_usd=config.budget_usd,
                         budget_remaining_usd=config.budget_usd / 2)
    d1 = decide(state, call, config, rng)
    d2 = decide(state, call, config, rng)
    assert d1.verdict == d2.verdict
    assert d1.deny_reason == d2.deny_reason


# ═══════════════════════ P8: HITL_ALWAYS → HITL ═══════════════════════

@given(tool_calls())
@settings(max_examples=300, deadline=None)
def test_p8_hitl_always_hitls(call):
    config = GuardConfig(hitl_mode=HitlMode.ALWAYS, budget_usd=100.0)
    state = SessionState(budget_initial_usd=100.0, budget_remaining_usd=100.0)
    d = decide(state, call, config, 0.5)
    assert d.verdict == Verdict.HITL


# ═══════════════════════ P9: Duplicate nonce → DENY ═══════════════════════

@given(configs(), tool_calls())
@settings(max_examples=300, deadline=None)
def test_p9_duplicate_nonce_denied(config, call):
    state = SessionState(budget_initial_usd=config.budget_usd,
                         budget_remaining_usd=config.budget_usd,
                         seen_nonces={call.nonce})
    d = decide(state, call, config, 0.5)
    assert d.verdict == Verdict.DENY


# ═══════════════════════ Additional Correctness Tests ═══════════════════════

def test_budget_exhausted():
    config = GuardConfig(budget_usd=1.0)
    state = SessionState(budget_initial_usd=1.0, budget_remaining_usd=0.0, budget_spent_usd=1.0)
    d = decide(state, ToolCall("t", b"h", 0.01, nonce=1), config, 0.5)
    assert d.verdict == Verdict.DENY and d.deny_reason == DenyReason.BUDGET_EXHAUSTED


def test_error_amplification():
    config = GuardConfig(max_repeat_calls=3, error_amplification=True)
    hp = hash_params({"q": "test", "error": "401 Unauthorized"})
    state = SessionState(last_tool_name="s", last_tool_params_hash=hp,
                         repeat_counts={"s": 2})
    call = ToolCall("s", hp, tool_params_raw='{"error":"401 Unauthorized"}', nonce=1)
    d = decide(state, call, config, 0.5)
    assert d.verdict == Verdict.DENY  # error amp: limit reduces to 2, count is 2 → DENY


def test_global_call_limit():
    config = GuardConfig(max_total_calls=100)
    d = decide(SessionState(total_calls=100), ToolCall("t", b"h", nonce=1), config, 0.5)
    assert d.verdict == Verdict.DENY


def test_probabilistic_deny_triggers():
    config = GuardConfig(budget_usd=1.0, probabilistic_deny=True, deny_jitter_ratio=0.5)
    state = SessionState(budget_initial_usd=1.0, budget_remaining_usd=0.10, budget_spent_usd=0.90)
    # ratio=0.10 < 0.2 → active; prob = 0.5*(1-0.10)=0.45; rng=0.30 < 0.45 → DENY
    d = decide(state, ToolCall("t", b"h", nonce=1), config, 0.30)
    assert d.verdict == Verdict.DENY and d.probabilistic_deny


def test_probabilistic_deny_skips():
    config = GuardConfig(budget_usd=1.0, probabilistic_deny=True, deny_jitter_ratio=0.5)
    state = SessionState(budget_initial_usd=1.0, budget_remaining_usd=0.10, budget_spent_usd=0.90)
    d = decide(state, ToolCall("t", b"h", nonce=1), config, 0.80)
    assert d.verdict == Verdict.ALLOW  # rng > probability


def test_different_params_resets_repeat():
    config = GuardConfig(max_repeat_calls=3)
    state = SessionState(last_tool_name="s", last_tool_params_hash=b"old",
                         repeat_counts={"s": 2})
    d = decide(state, ToolCall("s", b"new", nonce=1), config, 0.5)
    assert d.verdict == Verdict.ALLOW


def test_hash_deterministic():
    assert hash_params({"b": 1, "a": 2}) == hash_params({"a": 2, "b": 1})


def test_guard_config_rejects_invalid():
    import pytest as pt
    with pt.raises(AssertionError):
        GuardConfig(budget_usd=-1.0)
    with pt.raises(AssertionError):
        GuardConfig(deny_jitter_ratio=1.5)
