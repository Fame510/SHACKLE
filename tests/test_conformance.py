"""
Executable conformance harness for SHACKLE SP/1.0.
Runs the pure reference decide() against every fixture in
fixtures/conformance.json and asserts verdict + reason, verifies the
canonical hash of each fixture's call.params, and verifies detached SP/1.0.1
whole-vector seals without rewriting the byte-frozen published fixture file.

Usage:
    pytest tests/test_conformance.py -v
    # or without pytest:
    python tests/test_conformance.py
"""

import hashlib
import json
import os

from shackle.conformance import decide, canonical_hash, vector_hash

_HERE = os.path.dirname(os.path.abspath(__file__))
_FIXTURES = os.path.join(_HERE, os.pardir, "fixtures", "conformance.json")
_VECTOR_SEALS = os.path.join(_HERE, os.pardir, "fixtures", "conformance-vector-hashes.json")
_FIXTURES_101 = os.path.join(_HERE, os.pardir, "fixtures", "conformance-1.0.1.json")
# Exact published July 29 15-vector bytes, before the later inline license metadata.
_PUBLISHED_FIXTURE_SHA256 = "6553a1bced5ccab8c4c4f14d2f8a7c255383c2d5342a9a0fae375eab92a3e8da"


def _doc(path=_FIXTURES):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _load(path=_FIXTURES):
    return _doc(path)["fixtures"]


def _load_seals():
    with open(_VECTOR_SEALS, "r", encoding="utf-8") as fh:
        return json.load(fh)["fixtures"]


def test_published_fixture_file_is_byte_frozen():
    with open(_FIXTURES, "rb") as fh:
        actual = hashlib.sha256(fh.read()).hexdigest()
    assert actual == _PUBLISHED_FIXTURE_SHA256


def test_published_vector_count_is_fifteen():
    """The public claim is '15 vectors, independently reproduced'.

    The published fixture file stays byte-frozen. SP/1.0.1 integrity seals
    live in the detached conformance-vector-hashes.json sidecar, and new
    adversarial vectors live in conformance-1.0.1.json.
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
    """SP/1.0.1 seals each full vector out-of-band to preserve published bytes."""
    doc = _doc(_VECTOR_SEALS)
    assert doc["source_fixture_sha256"] == _PUBLISHED_FIXTURE_SHA256
    seals = doc["fixtures"]
    assert [entry["name"] for entry in seals] == [fx["name"] for fx in _load()]
    failures = []
    for fx, entry in zip(_load(), seals):
        got = vector_hash(fx)
        if got != entry["vector_hash"]:
            failures.append(f"{fx['name']}: vector_hash {got} != {entry['vector_hash']}")
    assert not failures, "Vector hash mismatches:\n" + "\n".join(failures)


def test_vector_hash_detects_expected_output_tampering():
    """The detached vector seal catches changes the params-only hash cannot."""
    fx = dict(_load()[0])
    seal = _load_seals()[0]["vector_hash"]
    assert canonical_hash(fx["call"]["params"]) == fx["canonical_hash"]
    tampered = dict(fx, expected_verdict="DENY")
    assert canonical_hash(tampered["call"]["params"]) == tampered["canonical_hash"]
    assert vector_hash(tampered) != seal


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
