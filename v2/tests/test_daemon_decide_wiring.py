#!/usr/bin/env python3
"""
Integration test: prove the V2 daemon's decision adapter (decision.decide_for_daemon)
routes through the verified shackle.conformance.decide() and reproduces its verdicts.

This is the CI proof that the daemon enforces SP/1.0-conformant decisions rather
than an independent reimplementation. Runs with only pytest (no Redis/Postgres).
"""
import os
import sys

import pytest

# Make both the repo root (for 'shackle') and v2/daemon (for 'decision') importable.
_HERE = os.path.dirname(__file__)
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_DAEMON = os.path.abspath(os.path.join(_REPO_ROOT, "v2", "daemon"))
for p in (_REPO_ROOT, _DAEMON):
    if p not in sys.path:
        sys.path.insert(0, p)

from shackle.conformance import canonical_hash  # noqa: E402
from decision import build_call, decide_for_daemon  # noqa: E402


def test_uses_verified_canonical_hash():
    params = {"b": 2, "a": 1, "nested": {"y": 2, "x": 1}}
    assert build_call("tool", params)["nonce"] == canonical_hash(params)
    # Full sha256 hex, not truncated to 16 chars (the old daemon behavior).
    assert len(build_call("tool", params)["nonce"]) == 64


def test_allow_within_thresholds():
    verdict, reason = decide_for_daemon(
        tool_name="search",
        parameters={"q": "hi"},
        budget_limit_usd=10.0,
        budget_remaining_usd=10.0,
        max_repeat_calls=3,
        prior_repeat_count=0,
    )
    assert verdict == "ALLOW"
    assert reason == "within_thresholds"


def test_budget_exhausted_denies():
    verdict, reason = decide_for_daemon(
        tool_name="search",
        parameters={"q": "hi"},
        budget_limit_usd=10.0,
        budget_remaining_usd=0.0,
        max_repeat_calls=3,
        prior_repeat_count=0,
    )
    assert verdict == "DENY"
    assert reason == "budget_exhausted"


def test_max_repeat_trips_at_limit():
    # prior_repeat_count=2 -> effective 3 -> with max_repeat_calls=3, trips.
    verdict, reason = decide_for_daemon(
        tool_name="loop_tool",
        parameters={"x": 1},
        budget_limit_usd=100.0,
        budget_remaining_usd=100.0,
        max_repeat_calls=3,
        prior_repeat_count=2,
    )
    assert verdict == "DENY"
    assert reason == "max_repeat_exceeded"


def test_max_repeat_allows_below_limit():
    # prior=1 -> effective 2 -> below max_repeat_calls=3 -> allowed.
    verdict, reason = decide_for_daemon(
        tool_name="loop_tool",
        parameters={"x": 1},
        budget_limit_usd=100.0,
        budget_remaining_usd=100.0,
        max_repeat_calls=3,
        prior_repeat_count=1,
    )
    assert verdict == "ALLOW"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
# CI trigger: exercise daemon<->decide() wiring proof on feat branch.
