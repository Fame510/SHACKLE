"""
Unit tests for the SHACKLE LiteLLM guardrails.

Covers Option A (pure conformance.decide()) and Option B (TriggerEngine-driven).
Pure Python; no litellm/redis/postgres needed (CustomGuardrail import is shimmed).

Run:
    pytest shackle/test_litellm_guardrail.py -q
"""

import pytest

from shackle.litellm_shackle_guardrail import (
    ShackleGuardrail,
    ShackleEngineGuardrail,
    ShackleBlocked,
)


def _req(model="gpt-4o", content="hello", tools=None):
    return {"model": model, "messages": [{"role": "user", "content": content}], "tools": tools or []}


def _usage(pt, ct):
    return {"usage": {"prompt_tokens": pt, "completion_tokens": ct}}


# ─────────────── Option A: pure decide() ───────────────

def test_a_allow_within_thresholds():
    g = ShackleGuardrail(budget_usd=1.0, max_repeat_calls=3)
    g.check(_req())  # should not raise
    assert g.state["repeat_counts"]["llm:gpt-4o"] == 1


def test_a_duplicate_nonce_denied():
    g = ShackleGuardrail(budget_usd=1.0, max_repeat_calls=99)
    g.check(_req(content="same"))
    with pytest.raises(ShackleBlocked) as e:
        g.check(_req(content="same"))  # identical -> same nonce -> replay DENY
    assert e.value.verdict == "DENY"
    assert "duplicate_nonce" in e.value.reason


def test_a_max_repeat_denied():
    g = ShackleGuardrail(budget_usd=100.0, max_repeat_calls=2)
    g.check(_req(content="a"))
    g.check(_req(content="b"))
    with pytest.raises(ShackleBlocked) as e:
        g.check(_req(content="c"))
    assert e.value.verdict == "DENY"
    assert e.value.reason == "max_repeat_exceeded"


def test_a_budget_exhausted_denied():
    g = ShackleGuardrail(budget_usd=0.01, max_repeat_calls=99)
    g.check(_req(content="x1"))
    g.record(_req(), _usage(100000, 100000))  # blow the budget
    with pytest.raises(ShackleBlocked) as e:
        g.check(_req(content="x2"))
    assert e.value.verdict == "DENY"
    assert e.value.reason == "budget_exhausted"


def test_a_hitl_always():
    g = ShackleGuardrail(budget_usd=1.0, hitl_mode="always")
    with pytest.raises(ShackleBlocked) as e:
        g.check(_req())
    assert e.value.verdict == "HITL"


def test_a_circuit_open_after_deny():
    g = ShackleGuardrail(budget_usd=0.01, max_repeat_calls=99)
    g.check(_req(content="y1"))
    g.record(_req(), _usage(100000, 100000))
    with pytest.raises(ShackleBlocked):
        g.check(_req(content="y2"))  # budget_exhausted -> trips circuit
    with pytest.raises(ShackleBlocked) as e:
        g.check(_req(content="y3"))  # now circuit_open
    assert e.value.reason == "circuit_open"


# ─────────────── Option B: TriggerEngine ───────────────

def test_b_allow_then_block_on_repeat():
    g = ShackleEngineGuardrail(budget=100.0, max_repeat_calls=2, timeout_seconds=1000)
    g.check(_req(content="loop"))  # count 1 -> ok
    with pytest.raises(ShackleBlocked) as e:
        g.check(_req(content="loop"))  # count 2 >= max_repeat -> REPETITIVE_TOOL_CALL
    assert e.value.reason == "REPETITIVE_TOOL_CALL"


def test_b_budget_exceeded_on_record():
    g = ShackleEngineGuardrail(budget=0.0001, max_repeat_calls=99, timeout_seconds=1000)
    g.check(_req(content="spend"))
    with pytest.raises(ShackleBlocked) as e:
        g.record(_req(), _usage(1000000, 1000000))  # huge spend -> BUDGET_OVERRUN
    # call cost (12.50) vastly exceeds the budget (0.0001), so decide()
    # catches it as a BUDGET_OVERRUN (the call would push remaining
    # negative) BEFORE any state is mutated -- strictly better than the
    # pre-fix behavior which would have mutated then raised BUDGET_EXCEEDED.
    assert e.value.reason == "BUDGET_OVERRUN"


def test_b_distinct_calls_do_not_trip():
    g = ShackleEngineGuardrail(budget=100.0, max_repeat_calls=3, timeout_seconds=1000)
    g.check(_req(content="one"))
    g.check(_req(content="two"))
    g.check(_req(content="three"))  # distinct inputs, no repeat trip
