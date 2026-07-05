"""
Executable conformance harness for SHACKLE SP/1.0.
Runs the pure reference decide() against every fixture in
fixtures/conformance.json and asserts verdict + reason, and verifies the
canonical hash of each fixture's call.params.

Usage:
    pytest tests/test_conformance.py -v
    # or without pytest:
    python tests/test_conformance.py
"""

import json
import os

from shackle.conformance import decide, canonical_hash

_HERE = os.path.dirname(os.path.abspath(__file__))
_FIXTURES = os.path.join(_HERE, os.pardir, "fixtures", "conformance.json")


def _load():
    with open(_FIXTURES, "r", encoding="utf-8") as fh:
        return json.load(fh)["fixtures"]


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


if __name__ == "__main__":
    test_all_fixtures_verdicts()
    test_all_fixtures_canonical_hashes()
    fixtures = _load()
    print(f"OK: {len(fixtures)} fixtures — all verdicts and hashes verified.")
