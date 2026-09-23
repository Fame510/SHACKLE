"""
Executable conformance harness for the DECISION-RESULT vectors.

Additive under the existing SP/1.0.1 revision — these vectors introduce no new
revision label and leave the published fixture files untouched.

fixtures/conformance.json pins the INPUT side of the decision surface
((config, state, call) -> verdict). This harness pins the CONSUMER side: given a
decision result produced by something else — a vendored decide(), a remote
daemon reply, a third-party reimplementation, a proxy that rewrote the payload —
what must the enforcement layer do?

Prior revisions proved fail-closed only for a decision function that RAISED.
These vectors cover the function that RETURNS something out of contract.

Usage:
    pytest tests/test_decision_result_conformance.py -v
    # or without pytest:
    python tests/test_decision_result_conformance.py
"""

import json
import os

from shackle.conformance import (
    DECISION_VERDICTS,
    SPEC_REVISION,
    normalize_decision,
    decide_checked,
    vector_hash,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
_FIXTURES_DR = os.path.join(
    _HERE, os.pardir, "fixtures", "decision-result-conformance.json"
)


class LyingStr(str):
    """A str subclass whose equality operator returns True for anything.

    Models a hostile or broken producer: `verdict == "ALLOW"` is True while the
    value is not the enum member. Defeated only by an exact-type check.
    """

    def __eq__(self, other):
        return True

    def __ne__(self, other):
        return False

    __hash__ = str.__hash__


def _decode(env):
    """Decode a language-neutral raw-result envelope (see the fixture doc)."""
    kind = env["kind"]
    if kind == "null":
        return None
    if kind == "string":
        return env["value"]
    if kind == "bytes":
        return env["value"].encode("utf-8")
    if kind in ("bool", "number"):
        return env["value"]
    if kind == "lying_string":
        return LyingStr(env["value"])
    if kind == "mapping":
        return {k: _decode(v) for k, v in env["entries"].items()}
    if kind == "sequence":
        items = [_decode(i) for i in env["items"]]
        return {"tuple": tuple, "list": list, "set": set}[env["container"]](items)
    raise ValueError("unknown envelope kind: " + kind)


def _doc():
    with open(_FIXTURES_DR, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _load():
    return _doc()["fixtures"]


def test_revision_matches_spec_revision():
    """These vectors are additive under the CURRENT revision, not a new one.

    Tied to SPEC_REVISION rather than a literal, so the fixture file and the
    implementation cannot drift apart, and so adding vectors never silently
    implies a revision bump.
    """
    assert _doc()["revision"] == SPEC_REVISION


def test_all_decision_result_vectors():
    failures = []
    for fx in _load():
        verdict, reason = normalize_decision(_decode(fx["raw"]))
        if verdict != fx["expected_verdict"] or reason != fx["expected_reason"]:
            failures.append(
                f"{fx['name']}: got {verdict}/{reason!r} "
                f"expected {fx['expected_verdict']}/{fx['expected_reason']!r}"
            )
    assert not failures, "Decision-result mismatches:\n" + "\n".join(failures)


def test_all_vector_hashes():
    failures = []
    for fx in _load():
        sealed = {k: v for k, v in fx.items() if k != "vector_hash"}
        got = vector_hash(sealed)
        if got != fx["vector_hash"]:
            failures.append(f"{fx['name']}: {got} != {fx['vector_hash']}")
    assert not failures, "vector_hash mismatches:\n" + "\n".join(failures)


def test_only_exact_allow_pairs_release():
    """THE invariant: exactly two fixture shapes may yield ALLOW.

    Anything else in the corpus must come back DENY or HITL. If a future
    relaxation (case-folding, trimming, dict coercion) leaks in, this fails.
    """
    allowed = [fx["name"] for fx in _load() if fx["expected_verdict"] == "ALLOW"]
    assert sorted(allowed) == ["result_valid_allow", "result_valid_allow_as_list"], (
        "Only an exact ('ALLOW', <usable reason>) ordered pair may release a "
        f"call; these vectors also released: {allowed}"
    )


def test_normalized_verdict_is_always_enforceable():
    """Every vector normalizes into the SP/1.0 enum — no third state exists."""
    for fx in _load():
        verdict, reason = normalize_decision(_decode(fx["raw"]))
        assert verdict in DECISION_VERDICTS, f"{fx['name']}: {verdict!r}"
        assert type(reason) is str and reason.strip(), f"{fx['name']}: {reason!r}"


def test_coercion_never_relaxes_a_verdict():
    """Coercion is one-directional: DENY/HITL can never become ALLOW."""
    for fx in _load():
        raw = _decode(fx["raw"])
        verdict, _ = normalize_decision(raw)
        if verdict != "ALLOW":
            continue
        # An ALLOW may only come from a raw result that literally said ALLOW.
        assert isinstance(raw, (tuple, list)) and raw[0] == "ALLOW", (
            f"{fx['name']}: normalization invented an ALLOW from {raw!r}"
        )


def test_hitl_is_never_downgraded_to_allow():
    verdict, _ = normalize_decision(("HITL", "fail_closed:opaque_context"))
    assert verdict == "HITL"


def test_normalize_decision_never_raises():
    """Total function: a guard that throws while validating fails open in
    practice, because the throw lands outside the enforcement branches."""
    class Explodes:
        def __len__(self):
            raise RuntimeError("boom")

        def __iter__(self):
            raise RuntimeError("boom")

        def __eq__(self, other):
            raise RuntimeError("boom")

        __hash__ = None

    for hostile in (Explodes(), object(), iter([1, 2]), (x for x in "ab")):
        verdict, reason = normalize_decision(hostile)
        assert verdict == "DENY", (hostile, verdict, reason)


def test_decide_checked_denies_on_raise():
    """The originally-proven case still holds through the new entry point."""
    def boom(config, state, call):
        raise RuntimeError("decision source unavailable")

    assert decide_checked(boom, {}, {}, {}) == (
        "DENY", "decide_unavailable_fail_closed")


def test_decide_checked_denies_on_base_exception():
    """A guard must not fail open on KeyboardInterrupt/SystemExit either."""
    def boom(config, state, call):
        raise SystemExit(1)

    assert decide_checked(boom, {}, {}, {}) == (
        "DENY", "decide_unavailable_fail_closed")


def test_decide_checked_validates_return_value():
    def liar(config, state, call):
        return "OK"

    assert decide_checked(liar, {}, {}, {}) == (
        "DENY", "malformed_decision:not_a_pair")


def test_decide_checked_passes_real_decide_through():
    from shackle.conformance import decide

    config = {"budget_usd": 10.0, "max_repeat_calls": 5}
    state = {
        "circuit_tripped": False,
        "seen_nonces": [],
        "budget_initial_usd": 10.0,
        "budget_remaining_usd": 10.0,
        "repeat_counts": {},
        "last_tool_name": None,
    }
    call = {"tool_name": "search", "params": {"q": "x"}, "estimated_cost_usd": 0.01}
    assert decide_checked(decide, config, state, call) == decide(config, state, call)


if __name__ == "__main__":
    vectors = _load()
    failures = []
    for fx in vectors:
        verdict, reason = normalize_decision(_decode(fx["raw"]))
        ok = verdict == fx["expected_verdict"] and reason == fx["expected_reason"]
        if not ok:
            failures.append(fx["name"])
        print(f"{'ok ' if ok else 'FAIL'} {fx['name']:40} -> {verdict}/{reason[:60]}")
    print(f"\n{len(vectors) - len(failures)}/{len(vectors)} decision-result "
          f"vectors verified ({SPEC_REVISION}).")
    raise SystemExit(1 if failures else 0)
