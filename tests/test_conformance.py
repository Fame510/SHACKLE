"""
Executable conformance harness for SHACKLE SP/1.0.
Runs the pure reference decide() against every fixture in
fixtures/conformance.json and asserts verdict + reason, verifies the
canonical hash of each fixture's call.params, and verifies the SP/1.0.1
vector_hash that seals each vector as a whole.

Usage:
    pytest tests/test_conformance.py -v
    # or without pytest:
    python tests/test_conformance.py
"""

import json
import os

from shackle.conformance import decide, canonical_hash, vector_hash

_HERE = os.path.dirname(os.path.abspath(__file__))
_FIXTURES = os.path.join(_HERE, os.pardir, "fixtures", "conformance.json")
_FIXTURES_101 = os.path.join(_HERE, os.pardir, "fixtures", "conformance-1.0.1.json")


def _doc(path=_FIXTURES):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _load(path=_FIXTURES):
    return _doc(path)["fixtures"]


def test_published_vector_count_is_fifteen():
    """The public claim is '15 vectors, independently reproduced'.

    SP/1.0.1 adds a field to each vector, not new vectors. New adversarial
    vectors live in fixtures/conformance-1.0.1.json so this count -- and the
    third-party reproductions that cite it -- stay exactly as published.
    """
    assert len(_load()) == 15


def test_all_fixtures_verdicts():
    failures = []
    for fx in _load():
        verdict, reason = decide(fx["config"], fx["state"], fx["call"])
        if verdict != fx["expected_verdict"] or reason != fx["expected_reason"]:
            failures.append(
                f"{fx['name']}: got {verdict}/{reason} "
                f"expected {fx['expected_verdict']}/{fx['expected_reason']}"
            )
    assert not failures, "Verdict mismatches:\n" + "\n".join(failures)


def test_all_fixtures_canonical_hashes():
    failures = []
    for fx in _load():
        got = canonical_hash(fx["call"]["params"])
        if got != fx["canonical_hash"]:
            failures.append(f"{fx['name']}: hash {got} != {fx['canonical_hash']}")
    assert not failures, "Hash mismatches:\n" + "\n".join(failures)


def test_all_fixtures_vector_hashes():
    """SP/1.0.1: the whole vector is sealed, not just the input preimage."""
    failures = []
    for fx in _load():
        if "vector_hash" not in fx:
            failures.append(f"{fx['name']}: missing vector_hash")
            continue
        got = vector_hash(fx)
        if got != fx["vector_hash"]:
            failures.append(f"{fx['name']}: vector_hash {got} != {fx['vector_hash']}")
    assert not failures, "Vector hash mismatches:\n" + "\n".join(failures)


def test_vector_hash_detects_expected_output_tampering():
    """The gap vector_hash closes: canonical_hash covers only call.params, so
    flipping an expected verdict left every published hash still verifying."""
    fx = dict(_load()[0])
    assert canonical_hash(fx["call"]["params"]) == fx["canonical_hash"]
    tampered = dict(fx, expected_verdict="DENY")
    assert canonical_hash(tampered["call"]["params"]) == tampered["canonical_hash"]
    assert vector_hash(tampered) != fx["vector_hash"]


# ── SP/1.0.1 adversarial vectors (separate file; the 15 stay 15) ──

def test_sp101_adversarial_vectors():
    doc = _doc(_FIXTURES_101)
    assert doc["revision"] == "SP/1.0.1"
    failures = []
    for fx in doc["fixtures"]:
        verdict, reason = decide(fx["config"], fx["state"], fx["call"])
        if verdict != fx["expected_verdict"] or reason != fx["expected_reason"]:
            failures.append(
                f"{fx['name']}: got {verdict}/{reason} "
                f"expected {fx['expected_verdict']}/{fx['expected_reason']}"
            )
        if vector_hash(fx) != fx["vector_hash"]:
            failures.append(f"{fx['name']}: vector_hash mismatch")
    assert not failures, "SP/1.0.1 mismatches:\n" + "\n".join(failures)


def test_sp101_vectors_are_a_strict_tightening():
    """SP/1.0.1 may only turn a previous ALLOW into a DENY/HITL.

    Any vector expecting ALLOW must be explicitly flagged as a negative control
    -- a probe that a conforming implementation must NOT block. Without that
    flag an ALLOW here would mean the revision loosened something, which the
    revision note says it does not.
    """
    for fx in _doc(_FIXTURES_101)["fixtures"]:
        if fx.get("negative_control"):
            assert fx["expected_verdict"] == "ALLOW", fx["name"]
        else:
            assert fx["expected_verdict"] in ("DENY", "HITL"), fx["name"]


if __name__ == "__main__":
    test_published_vector_count_is_fifteen()
    test_all_fixtures_verdicts()
    test_all_fixtures_canonical_hashes()
    test_all_fixtures_vector_hashes()
    test_vector_hash_detects_expected_output_tampering()
    test_sp101_adversarial_vectors()
    test_sp101_vectors_are_a_strict_tightening()
    print(
        f"OK: {len(_load())} SP/1.0 vectors + "
        f"{len(_doc(_FIXTURES_101)['fixtures'])} SP/1.0.1 adversarial vectors verified."
    )
