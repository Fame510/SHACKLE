"""
SHACKLE Combined-Trigger Conformance Runner — SP/1.0 (fix/audit-hardening)
==========================================================================
Runs fixtures/conformance_combined.json — vectors where TWO OR MORE guard
conditions are simultaneously active — against BOTH reference cores:

  * v2/spec/decide.py::decide            (typed core)
  * shackle/conformance.py::decide       (dict core; imported by the shipped runtime)

The single-trigger fixtures/conformance.json never exercised layer precedence,
which is the only place the two cores previously disagreed (an adversarial audit
found an approved-then-replayed nonce ALLOWed by the typed core while the dict
core correctly DENIED it as a replay). This runner pins the canonical precedence
and FAILS if the two cores ever diverge again.
"""
import json
import os
import sys

# typed core (../spec)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "spec"))
from decide import (  # noqa: E402
    GuardConfig, SessionState, ToolCall, Decision,
    Verdict, DenyReason, HitlMode, decide as decide_typed, hash_params,
)

# dict core (repo root: shackle/conformance.py)
_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, _ROOT)
from shackle.conformance import decide as decide_dict  # noqa: E402

FIXTURES = os.path.join(_ROOT, "fixtures", "conformance_combined.json")

_HITL_MODES = {
    "never": HitlMode.NEVER,
    "on_deny": HitlMode.ON_DENY,
    "on_threshold": HitlMode.ON_THRESHOLD,
    "always": HitlMode.ALWAYS,
}


def _build_config(c):
    kw = dict(c)
    if "hitl_mode" in kw:
        kw["hitl_mode"] = _HITL_MODES[str(kw["hitl_mode"]).lower()]
    allowed = GuardConfig().__dict__.keys()
    kw = {k: v for k, v in kw.items() if k in allowed}
    return GuardConfig(**kw)


def _params_hash(params):
    if isinstance(params, dict) and params.get("__noncanonical__") is not True:
        return hash_params(params)
    return b""


def _build_call(call):
    params = call.get("params", {})
    return ToolCall(
        tool_name=call.get("tool_name", ""),
        tool_params_hash=_params_hash(params),
        estimated_cost_usd=call.get("estimated_cost_usd", 0.0),
        nonce=call.get("nonce", 0),
        tool_params=params,
    )


def _build_state(s, call):
    kw = dict(s)
    if "seen_nonces" in kw:
        kw["seen_nonces"] = set(kw["seen_nonces"])
    allowed = SessionState().__dict__.keys()
    kw = {k: v for k, v in kw.items() if k in allowed}
    st = SessionState(**kw)
    if (not st.last_tool_params_hash and st.last_tool_name
            and st.last_tool_name == call.tool_name):
        st.last_tool_params_hash = call.tool_params_hash
    return st


def _reason_string(d):
    hr = (d.human_readable or "").lower()
    if "[hitl_transition:duplicate_resume]" in hr:
        return "policy_violation:duplicate_resume_no_effect"
    if "[hitl_transition:reject]" in hr:
        return "hitl_transition:reject"
    if "[hitl_transition:defer_escalate]" in hr:
        return "hitl_transition:defer_escalate"
    if "[hitl_transition:approve]" in hr:
        return "hitl_transition:approve"
    if "[hitl_transition:modify_successor]" in hr:
        return "hitl_transition:modify_successor"
    if d.verdict == Verdict.ALLOW:
        return "within_thresholds"
    if d.verdict == Verdict.DENY:
        if "[malformed_input]" in hr:
            return "policy_violation:malformed_input"
        if d.deny_reason == DenyReason.POLICY_VIOLATION:
            return "policy_violation:duplicate_nonce"
        return d.deny_reason.value
    if "[opaque_context]" in hr:
        return "fail_closed:opaque_context"
    if "threshold" in hr:
        return "budget_threshold"
    if "all calls" in hr:
        return "hitl_all_calls"
    return hr


def _load():
    with open(FIXTURES) as f:
        return json.load(f)["fixtures"]


def test_combined_fixtures_typed_core():
    """Typed core (v2/spec/decide.py) must reproduce every combined vector."""
    fixtures = _load()
    assert fixtures, "no combined fixtures loaded"
    failures = []
    for fx in fixtures:
        config = _build_config(fx.get("config", {}))
        call = _build_call(fx.get("call", {}))
        state = _build_state(fx.get("state", {}), call)
        d = decide_typed(state, call, config, rng_float=0.0)
        got = (d.verdict.value, _reason_string(d))
        exp = (fx["expected_verdict"], fx["expected_reason"])
        if got != exp:
            failures.append(f"{fx['name']}: expected {exp} got {got}")
    assert not failures, "Typed-core combined failures:\n" + "\n".join(failures)


def test_combined_fixtures_dict_core():
    """Dict core (shackle/conformance.py — shipped runtime) must reproduce every vector."""
    fixtures = _load()
    failures = []
    for fx in fixtures:
        verdict, reason = decide_dict(fx.get("config", {}), fx.get("state", {}), fx.get("call", {}))
        got = (verdict, reason)
        exp = (fx["expected_verdict"], fx["expected_reason"])
        if got != exp:
            failures.append(f"{fx['name']}: expected {exp} got {got}")
    assert not failures, "Dict-core combined failures:\n" + "\n".join(failures)


def test_two_cores_agree_on_combined():
    """ANTI-DIVERGENCE GUARD: both cores must return identical verdict+reason
    on every combined vector. This is the regression test for the audit finding
    that the two cores disagreed on precedence (notably replay-vs-approval)."""
    fixtures = _load()
    diverged = []
    for fx in fixtures:
        config = _build_config(fx.get("config", {}))
        call = _build_call(fx.get("call", {}))
        state = _build_state(fx.get("state", {}), call)
        dt = decide_typed(state, call, config, rng_float=0.0)
        typed = (dt.verdict.value, _reason_string(dt))
        dv = decide_dict(fx.get("config", {}), fx.get("state", {}), fx.get("call", {}))
        if typed != dv:
            diverged.append(f"{fx['name']}: typed={typed} dict={dv}")
    assert not diverged, "CORES DIVERGE on combined vectors:\n" + "\n".join(diverged)


if __name__ == "__main__":
    test_combined_fixtures_typed_core()
    test_combined_fixtures_dict_core()
    test_two_cores_agree_on_combined()
    print("OK: combined-trigger conformance passes on both cores; cores agree.")
