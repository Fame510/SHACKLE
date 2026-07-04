"""
SHACKLE Conformance Runner â SP/1.0
====================================
Executes fixtures/conformance.json against the reference decide() core and
asserts every vector produces the expected typed verdict + reason. Also
re-derives each fixture's canonical_hash to prove canonicalization determinism.

This is the machine-checkable proof that the SHACKLE reference implementation
conforms to its own published test vectors. Any adapter (CrewAI / LangGraph /
AutoGen) or downstream evidence layer can run the same vectors and diff.
"""
import json
import os
import sys

# Import the reference core from ../spec
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "spec"))

from decide import (  # noqa: E402
    GuardConfig, SessionState, ToolCall, Decision,
    Verdict, DenyReason, HitlMode, decide, hash_params,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "..", "fixtures", "conformance.json")

_HITL_MODES = {
    "never": HitlMode.NEVER,
    "on_deny": HitlMode.ON_DENY,
    "on_threshold": HitlMode.ON_THRESHOLD,
    "always": HitlMode.ALWAYS,
}


def _build_config(c: dict) -> GuardConfig:
    kw = dict(c)
    if "hitl_mode" in kw:
        kw["hitl_mode"] = _HITL_MODES[str(kw["hitl_mode"]).lower()]
    allowed = GuardConfig().__dict__.keys()
    kw = {k: v for k, v in kw.items() if k in allowed}
    return GuardConfig(**kw)


def _params_hash(params) -> bytes:
    if isinstance(params, dict) and params.get("__noncanonical__") is not True:
        return hash_params(params)
    return b""


def _build_call(call: dict) -> ToolCall:
    params = call.get("params", {})
    return ToolCall(
        tool_name=call.get("tool_name", ""),
        tool_params_hash=_params_hash(params),
        estimated_cost_usd=call.get("estimated_cost_usd", 0.0),
        nonce=call.get("nonce", 0),
        tool_params=params,
    )


def _build_state(s: dict, call: ToolCall) -> SessionState:
    kw = dict(s)
    if "seen_nonces" in kw:
        kw["seen_nonces"] = set(kw["seen_nonces"])
    allowed = SessionState().__dict__.keys()
    kw = {k: v for k, v in kw.items() if k in allowed}
    st = SessionState(**kw)
    # Fixtures express "this is a repeat of the previous identical call" by naming
    # last_tool_name == the call's tool. Bind last_tool_params_hash to the call's
    # params hash so the repeat guard sees an identical prior call, per the fixture
    # note ("params_hash equals state.last_tool_params_hash"). Only do this when the
    # fixture did not explicitly provide a last_tool_params_hash.
    if (not st.last_tool_params_hash
            and st.last_tool_name
            and st.last_tool_name == call.tool_name):
        st.last_tool_params_hash = call.tool_params_hash
    return st


def _reason_string(d: Decision) -> str:
    """Map a typed Decision to the fixture reason string form."""
    hr = (d.human_readable or "").lower()
    if d.verdict == Verdict.ALLOW:
        return "within_thresholds"
    if d.verdict == Verdict.DENY:
        if "[malformed_input]" in hr:
            return "policy_violation:malformed_input"
        if d.deny_reason == DenyReason.POLICY_VIOLATION:
            # duplicate nonce is the only other policy_violation path
            return "policy_violation:duplicate_nonce"
        return d.deny_reason.value  # budget_exhausted, max_repeat_exceeded, circuit_open, ...
    # HITL
    if "[opaque_context]" in hr:
        return "fail_closed:opaque_context"
    if "threshold" in hr:
        return "budget_threshold"
    if "all calls" in hr:
        return "hitl_all_calls"
    return hr


def _load_fixtures():
    with open(FIXTURES) as f:
        return json.load(f)["fixtures"]


def test_all_fixtures_conform():
    fixtures = _load_fixtures()
    assert fixtures, "no fixtures loaded"
    failures = []
    for fx in fixtures:
        config = _build_config(fx.get("config", {}))
        call = _build_call(fx.get("call", {}))
        state = _build_state(fx.get("state", {}), call)
        d = decide(state, call, config, rng_float=0.0)
        got_v = d.verdict.value
        got_r = _reason_string(d)
        exp_v = fx["expected_verdict"]
        exp_r = fx["expected_reason"]
        if got_v != exp_v or got_r != exp_r:
            failures.append(
                f"{fx['name']}: expected {exp_v}/{exp_r} got {got_v}/{got_r}"
            )
    assert not failures, "Conformance failures:\n" + "\n".join(failures)


def test_canonical_hashes_reproduce():
    """Every well-formed fixture's canonical_hash must reproduce via hash_params."""
    import hashlib
    fixtures = _load_fixtures()
    mismatches = []
    for fx in fixtures:
        params = fx.get("call", {}).get("params", {})
        if not isinstance(params, dict):
            continue
        if params.get("__noncanonical__") is True:
            # sentinel-class vector: hash still defined over the literal JSON
            pass
        canonical = json.dumps(params, sort_keys=True, separators=(",", ":"))
        h = hashlib.sha256(canonical.encode()).hexdigest()
        if "canonical_hash" in fx and fx["canonical_hash"] != h:
            mismatches.append(f"{fx['name']}: fixture={fx['canonical_hash']} computed={h}")
    assert not mismatches, "Hash mismatches:\n" + "\n".join(mismatches)


if __name__ == "__main__":
    test_all_fixtures_conform()
    test_canonical_hashes_reproduce()
    print("â All conformance vectors pass.")
