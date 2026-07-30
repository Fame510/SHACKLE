"""
SP/1.0.1 regression tests.

One test (or group) per audit finding. These cover the behaviours that a
language-neutral JSON fixture CANNOT express -- NaN floats, non-string dict
keys, unserializable objects, wall-clock behaviour, and the runtime's
enforcement of decide() -- and so are the tests that stop each fix from
silently rotting back into a magic-literal special case.
"""

import threading
import time

import pytest

from shackle.conformance import (
    canonical_hash,
    canonicalization_error,
    decide,
    evaluate_transition,
    is_canonicalizable,
    opaque_context_key,
    vector_hash,
)
from shackle.core import (
    ExecutionState,
    ShackleInterrupt,
    TriggerEngine,
)


BASE_CFG = {"budget_usd": 10.0, "max_repeat_calls": 5}


def _state(**over):
    st = {
        "circuit_tripped": False,
        "seen_nonces": [],
        "budget_initial_usd": 10.0,
        "budget_remaining_usd": 10.0,
        "repeat_counts": {},
        "last_tool_name": None,
        "pending_transition": None,
    }
    st.update(over)
    return st


# ══════════════════════════════════════════════════════════════
# Issue 4 — canonicalization detected STRUCTURALLY, not by marker
# ══════════════════════════════════════════════════════════════

class TestStructuralCanonicalization:
    """The SP/1.0 test was `params.get("__noncanonical__") is True`. A payload
    that is genuinely non-canonicalizable but omits the marker sailed through."""

    @pytest.mark.parametrize("params,token", [
        ({"x": float("nan")}, "non_finite_number"),
        ({"x": float("inf")}, "non_finite_number"),
        ({"x": float("-inf")}, "non_finite_number"),
        ({"x": {"y": [1, float("nan")]}}, "non_finite_number"),
        ({1: "int key"}, "non_string_key"),
        ({"x": {2.5: "float key"}}, "non_string_key"),
        ({"x": {None: "none key"}}, "non_string_key"),
        ({"x": {1, 2, 3}}, "unsupported_type:set"),
        ({"x": b"bytes"}, "unsupported_type:bytes"),
        ({"x": object()}, "unsupported_type:object"),
    ])
    def test_rejected_without_any_marker(self, params, token):
        assert "__noncanonical__" not in params
        assert canonicalization_error(params) == token
        assert not is_canonicalizable(params)
        assert decide(BASE_CFG, _state(), {"tool_name": "t", "params": params}) == (
            "DENY", "policy_violation:malformed_input",
        )

    def test_non_object_params_denied_not_crashed(self):
        """HEAD raised AttributeError on a list; decide() must be total."""
        for params in (["a"], "str", 42, None):
            call = {"tool_name": "t", "params": params}
            # `or {}` in decide() turns falsy params into {}, which is a valid
            # empty object; only genuinely non-object truthy params are denied.
            expected = ("ALLOW", "within_thresholds") if not params else (
                "DENY", "policy_violation:malformed_input")
            assert decide(BASE_CFG, _state(), call) == expected

    def test_deep_nesting_is_bounded(self):
        deep = cur = {}
        for _ in range(200):
            cur["n"] = {}
            cur = cur["n"]
        assert canonicalization_error(deep) == "max_depth_exceeded"

    def test_disclosed_sentinel_still_honoured(self):
        """The language-neutral fixture relies on it; it must remain valid."""
        assert canonicalization_error({"__noncanonical__": True}) == (
            "declared_non_canonicalizable")

    def test_ordinary_params_unaffected(self):
        ok = {"a": 1, "b": "two", "c": [1, 2, {"d": None}], "e": True, "f": 1.5}
        assert canonicalization_error(ok) is None
        assert canonical_hash(ok) == canonical_hash(dict(reversed(list(ok.items()))))


# ══════════════════════════════════════════════════════════════
# Issue 5 — opaque context is a key-class x value-class product
# ══════════════════════════════════════════════════════════════

class TestOpaqueContext:
    @pytest.mark.parametrize("params", [
        {"ctx": "opaque"},
        {"context": "opaque"},
        {"raw_context": "unevaluable"},
        {"blob": "untestable"},
        {"opaque": "unknown"},
        {"CTX": "OPAQUE"},                      # case-insensitive
        {"ctx": "  opaque  "},                  # whitespace-insensitive
        {"outer": {"context": "opaque"}},       # nested dict
        {"items": [{"ctx": "unknown"}]},        # nested list
    ])
    def test_fails_closed_to_hitl(self, params):
        assert opaque_context_key(params) is not None
        assert decide(BASE_CFG, _state(), {"tool_name": "t", "params": params}) == (
            "HITL", "fail_closed:opaque_context",
        )

    def test_uninspectable_value_denies_rather_than_escalates(self):
        """A value the guard cannot introspect is BOTH opaque (rule 9, HITL) and
        non-canonicalizable (rule 1, DENY). Rule 1 has higher precedence, so the
        stricter verdict wins. Both paths are closed; assert which one fires so
        the precedence is pinned rather than incidental."""
        params = {"ctx": object()}
        assert opaque_context_key(params) is not None
        assert decide(BASE_CFG, _state(), {"tool_name": "t", "params": params}) == (
            "DENY", "policy_violation:malformed_input",
        )

    @pytest.mark.parametrize("params", [
        {"context": "user asked about pricing"},   # evaluable context
        {"ctx": "checkout_page"},
        {"blob": {"size": 12}},                    # structured, introspectable
        {"description": "opaque"},                 # opaque value, non-context key
        {"tool": "click", "target": "#submit"},
    ])
    def test_evaluable_context_is_not_blocked(self, params):
        """Over-blocking is a failure too: a disjunctive rule ('any ctx key' or
        'any opaque value') would HITL all of these and make the guard useless."""
        assert opaque_context_key(params) is None
        assert decide(BASE_CFG, _state(), {"tool_name": "t", "params": params}) == (
            "ALLOW", "within_thresholds",
        )


# ══════════════════════════════════════════════════════════════
# Issue 1 — HITL transition contract is bound and single-use
# ══════════════════════════════════════════════════════════════

class TestTransitionContract:
    APPROVED = {"amount": 100, "to": "acct_1"}
    DIGEST = canonical_hash(APPROVED)

    def _approve(self, **over):
        pending = {"decision": "approve", "original_nonce": 7,
                   "original_args_digest": self.DIGEST, "terminal_status": "pending"}
        pending.update(over)
        return pending

    def test_exact_authorized_pair_is_released(self):
        assert evaluate_transition(
            self._approve(), {"nonce": 7}, self.DIGEST,
        ) == ("ALLOW", "hitl_transition:approve")

    def test_tampered_arguments_denied(self):
        other = canonical_hash({"amount": 100000, "to": "acct_attacker"})
        assert evaluate_transition(self._approve(), {"nonce": 7}, other) == (
            "DENY", "hitl_transition:digest_mismatch")

    def test_unrelated_call_cannot_ride_the_approval(self):
        assert evaluate_transition(self._approve(), {"nonce": 999}, self.DIGEST) == (
            "DENY", "hitl_transition:nonce_mismatch")

    def test_approval_is_single_use(self):
        assert evaluate_transition(
            self._approve(terminal_status="consumed"), {"nonce": 7}, self.DIGEST,
        ) == ("DENY", "hitl_transition:terminal_no_effect")

    def test_unbound_approval_refused(self):
        assert evaluate_transition(
            {"decision": "approve"}, {"nonce": 7}, self.DIGEST,
        ) == ("DENY", "hitl_transition:unbound_authorization")

    def test_unknown_decision_fails_closed(self):
        for d in ("approve_with_conditions", "", None, 42, "APPROVED?"):
            verdict, reason = evaluate_transition(
                {"decision": d, "original_nonce": 7}, {"nonce": 7}, self.DIGEST)
            assert verdict == "DENY", d
            assert reason == "hitl_transition:unknown_decision"

    def test_modify_supersedes_the_original(self):
        successor = canonical_hash({"amount": 50, "to": "acct_1"})
        pending = {"decision": "modify", "original_nonce": 7,
                   "original_args_digest": self.DIGEST, "successor_nonce": 8,
                   "successor_args_digest": successor, "terminal_status": "superseded"}
        # the original must not execute...
        assert evaluate_transition(pending, {"nonce": 7}, self.DIGEST) == (
            "DENY", "hitl_transition:superseded_original")
        # ...only the successor, and only with the successor's arguments.
        assert evaluate_transition(pending, {"nonce": 8}, successor) == (
            "ALLOW", "hitl_transition:modify_successor")
        assert evaluate_transition(pending, {"nonce": 8}, self.DIGEST) == (
            "DENY", "hitl_transition:digest_mismatch")

    def test_history_visible_is_not_runtime_executable(self):
        """The headline SP/1.0 invariant, as an executable assertion."""
        pending = self._approve(terminal_status="consumed")
        assert pending["original_nonce"] == 7             # visible in history
        assert evaluate_transition(pending, {"nonce": 7}, self.DIGEST)[0] == "DENY"


# ══════════════════════════════════════════════════════════════
# Issue 10 — vector_hash seals the whole vector
# ══════════════════════════════════════════════════════════════

class TestVectorHash:
    VECTOR = {
        "name": "v", "config": BASE_CFG, "state": _state(),
        "call": {"tool_name": "t", "params": {"a": 1}},
        "canonical_hash": canonical_hash({"a": 1}),
        "expected_verdict": "ALLOW", "expected_reason": "within_thresholds",
    }

    def test_is_self_excluding_and_stable(self):
        h = vector_hash(self.VECTOR)
        assert vector_hash(dict(self.VECTOR, vector_hash=h)) == h

    @pytest.mark.parametrize("field,value", [
        ("expected_verdict", "DENY"),
        ("expected_reason", "circuit_open"),
        ("name", "renamed"),
        ("config", {"budget_usd": 0.0}),
    ])
    def test_detects_tampering_canonical_hash_cannot_see(self, field, value):
        tampered = dict(self.VECTOR, **{field: value})
        # canonical_hash covers only call.params, so it is still "valid"...
        assert canonical_hash(tampered["call"]["params"]) == tampered["canonical_hash"]
        # ...but the vector as a whole no longer verifies.
        assert vector_hash(tampered) != vector_hash(self.VECTOR)


# ══════════════════════════════════════════════════════════════
# Issues 2 & 3 — the runtime ENFORCES decide() on real state
# ══════════════════════════════════════════════════════════════

class TestRuntimeEnforcesDecide:
    def test_latched_circuit_denies_tool_calls(self):
        """circuit_tripped was hardcoded False into decide(), so decide() could
        never return circuit_open on the tool path no matter the runtime state."""
        engine = TriggerEngine(budget=100.0, max_repeat_calls=99, timeout_seconds=1000)
        state = ExecutionState()
        engine.evaluate_tool_call("a", "search", "q", state)
        state.trip_circuit("manual")
        with pytest.raises(ShackleInterrupt) as e:
            engine.evaluate_tool_call("a", "search", "different", state)
        assert e.value.trigger_type == "CIRCUIT_OPEN"
        assert state.last_decision == ("DENY", "circuit_open")

    def test_resume_is_the_only_thing_that_unlatches(self):
        state = ExecutionState()
        state.trip_circuit("boom")
        assert state.circuit_tripped and state.circuit_trip_reason == "boom"
        state.reset_circuit()
        assert not state.circuit_tripped and state.circuit_trip_reason == ""

    def test_replayed_nonce_denied_on_tool_path(self):
        engine = TriggerEngine(budget=100.0, max_repeat_calls=99, timeout_seconds=1000)
        state = ExecutionState()
        engine.evaluate_tool_call("a", "search", "q1", state, nonce="n-1")
        assert "n-1" in state.seen_nonces
        with pytest.raises(ShackleInterrupt) as e:
            engine.evaluate_tool_call("a", "search", "q2", state, nonce="n-1")
        assert e.value.trigger_type == "DUPLICATE_NONCE"

    def test_opaque_tool_input_fails_closed(self):
        """params was hardcoded {} into decide(), so the opaque-context rule was
        unreachable from the tool path and an unevaluable call ran anyway."""
        engine = TriggerEngine(budget=100.0, max_repeat_calls=99, timeout_seconds=1000)
        state = ExecutionState()
        with pytest.raises(ShackleInterrupt) as e:
            engine.evaluate_tool_call("a", "browse", {"ctx": "opaque"}, state)
        assert e.value.trigger_type == "HITL_REQUIRED"
        assert state.last_decision == ("HITL", "fail_closed:opaque_context")

    def test_non_canonicalizable_tool_input_denied(self):
        engine = TriggerEngine(budget=100.0, max_repeat_calls=99, timeout_seconds=1000)
        state = ExecutionState()
        with pytest.raises(ShackleInterrupt) as e:
            engine.evaluate_tool_call("a", "calc", {"x": float("nan")}, state)
        assert e.value.trigger_type == "POLICY_VIOLATION"
        assert state.last_decision == ("DENY", "policy_violation:malformed_input")

    def test_verdict_and_enforcement_never_disagree(self):
        """The core defect behind issue 2: decide() said one thing, the runtime
        did another. Whatever decide() returns, a non-ALLOW must raise."""
        engine = TriggerEngine(budget=100.0, max_repeat_calls=2, timeout_seconds=1000)
        state = ExecutionState()
        engine.evaluate_tool_call("a", "search", "loop", state)
        assert state.last_decision[0] == "ALLOW"
        with pytest.raises(ShackleInterrupt) as e:
            engine.evaluate_tool_call("a", "search", "loop", state)
        assert state.last_decision == ("DENY", "max_repeat_exceeded")
        assert e.value.trigger_type == "REPETITIVE_TOOL_CALL"
        assert e.value.details["error_loop"] is False
        assert e.value.details["decide_reason"] == "max_repeat_exceeded"

    def test_tool_path_enforces_budget(self):
        engine = TriggerEngine(budget=1.0, max_repeat_calls=99, timeout_seconds=1000)
        state = ExecutionState()
        state.total_cost = 0.90
        with pytest.raises(ShackleInterrupt) as e:
            engine.evaluate_tool_call("a", "expensive", "x", state,
                                      estimated_cost_usd=0.50)
        assert e.value.trigger_type == "BUDGET_OVERRUN"


# ══════════════════════════════════════════════════════════════
# Issue 8 — wrapper fixes
# ══════════════════════════════════════════════════════════════

class TestWrapperFixes:
    def test_wrap_tool_enforces_budget(self):
        from shackle.autogen_shackle_wrapper import wrap_tool, ShackleBlocked

        @wrap_tool(budget=1.0, max_repeat_calls=99, timeout_seconds=1000,
                   cost_per_call=0.40)
        def priced(x):
            return x

        assert priced("a") == "a"   # spend 0.40, remaining 0.60
        assert priced("b") == "b"   # spend 0.40, remaining 0.20
        with pytest.raises(ShackleBlocked) as e:
            priced("c")             # 0.40 > 0.20 remaining -> overrun
        assert e.value.trigger_type == "BUDGET_OVERRUN"

    def test_wrap_tool_clock_starts_at_first_call_not_import(self):
        """ExecutionState.start_time was set when the decorator ran, i.e. at
        import. A process that imported its tools then idled tripped
        TIMEOUT_REACHED on the very first real call."""
        from shackle.autogen_shackle_wrapper import wrap_tool

        @wrap_tool(budget=100.0, max_repeat_calls=99, timeout_seconds=0.20)
        def slow_to_be_used(x):
            return x

        time.sleep(0.35)            # longer than the timeout, before any call
        assert slow_to_be_used("ok") == "ok"

    def test_wrap_tool_reset_clears_state(self):
        from shackle.autogen_shackle_wrapper import wrap_tool, ShackleBlocked

        @wrap_tool(budget=100.0, max_repeat_calls=2, timeout_seconds=1000)
        def tool(x):
            return x

        tool("same")
        with pytest.raises(ShackleBlocked):
            tool("same")
        tool.shackle_reset()
        assert tool("same") == "same"

    def test_litellm_nonce_is_per_dispatch(self):
        from shackle.litellm_shackle_guardrail import ShackleGuardrail

        g = ShackleGuardrail(budget_usd=1.0, max_repeat_calls=99)
        req = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
        a, b = g._build_call(req), g._build_call(req)
        assert a["nonce"] != b["nonce"]                  # replay token: unique
        assert a["args_digest"] == b["args_digest"]      # content digest: stable
        assert a["args_digest"] == canonical_hash(a["params"])


# ══════════════════════════════════════════════════════════════
# Determinism / totality of the decision surface
# ══════════════════════════════════════════════════════════════

def test_spec_documents_every_reason_the_core_can_return():
    """Issue 9 was documentation drift: the spec described a different function.

    Pin the interface that matters -- the reason vocabulary -- so §3.5 and
    shackle/conformance.py cannot diverge again without a red test.
    """
    import os
    import re

    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
    with open(os.path.join(root, "shackle", "conformance.py"), encoding="utf-8") as fh:
        impl = fh.read()
    with open(os.path.join(root, "SP-1.0-SPECIFICATION.md"), encoding="utf-8") as fh:
        spec = fh.read()

    emitted = set(re.findall(r'return \("(?:ALLOW|DENY|HITL)", "([^"]+)"\)', impl))
    emitted |= set(re.findall(r'allow_reason = "([^"]+)"', impl))
    assert len(emitted) >= 20, f"reason extraction looks broken: {sorted(emitted)}"

    table = spec.split("### 3.5 Reason Vocabulary")[1].split("### 3.6")[0]
    documented = set(re.findall(r"^\| `([^`]+)` \|", table, re.M))

    assert not emitted - documented, (
        "reasons the core returns but the spec does not document: "
        f"{sorted(emitted - documented)}")
    assert not documented - emitted, (
        "reasons the spec documents but the core cannot return: "
        f"{sorted(documented - emitted)}")


def test_decide_is_deterministic_under_concurrency():
    call = {"tool_name": "t", "params": {"a": [1, {"b": 2}]}, "nonce": 1}
    expected = decide(BASE_CFG, _state(), call)
    results = []

    def worker():
        for _ in range(50):
            results.append(decide(BASE_CFG, _state(), call))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(results) == {expected}
