#!/usr/bin/env python3
"""
SHACKLE Sovereign Daemon - Verified decision adapter.

This module is the SINGLE decision authority for the daemon. It maps the
daemon's Redis-derived session state into the (config, state, call) shape
consumed by shackle.conformance.decide() -- the pure reference function that
fixtures/conformance.json encodes. The daemon MUST route every enforcement
decision through decide_for_daemon() so that the enforced verdict is, by
construction, the SP/1.0-conformant verdict.

Copyright (C) 2026  Dante Bullock, Sovereign Logic.  AGPL-3.0-or-later.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, Tuple

# Import the verified, dependency-free reference decision surface. The daemon
# runs from v2/daemon/, so the repo root (which contains the 'shackle' package)
# must be importable. We add it explicitly rather than relying on install state.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from shackle.conformance import canonical_hash, decide  # noqa: E402

Verdict = str  # "ALLOW" | "DENY" | "HITL"


def build_call(tool_name: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
    """Construct the canonical 'call' dict, including the verified nonce.

    Uses the SAME canonical_hash the conformance vectors use, so the daemon's
    replay/repeat identity matches the spec exactly (previously the daemon used
    a truncated 16-char sha256 with loose separators -- a different function).
    """
    return {
        "tool_name": tool_name,
        "params": parameters,
        "nonce": canonical_hash(parameters),
    }


def decide_for_daemon(
    *,
    tool_name: str,
    parameters: Dict[str, Any],
    budget_limit_usd: float,
    budget_remaining_usd: float,
    max_repeat_calls: int,
    prior_repeat_count: int,
    estimated_cost: float = 0.0,
    hitl_mode: str = "never",
) -> Tuple[Verdict, str]:
    """Produce the verified (verdict, reason) for one pre-exec evaluation.

    prior_repeat_count is the number of identical calls ALREADY recorded for
    this session (not counting the current one). We add 1 to represent the
    call now under evaluation, so the daemon trips at exactly max_repeat_calls
    -- matching decide()'s 'rc >= max_repeat' semantics and eliminating the
    previous off-by-N divergence between the daemon and the spec.
    """
    config: Dict[str, Any] = {
        "budget_usd": budget_limit_usd,
        "max_repeat_calls": max_repeat_calls,
        "hitl_mode": hitl_mode,
    }
    effective_count = prior_repeat_count + 1
    # Deduct this call's estimated cost so a call that would exceed the budget is
    # denied up front (matches the daemon's pre-flight budget gate).
    remaining_after = budget_remaining_usd - estimated_cost
    state: Dict[str, Any] = {
        "budget_remaining_usd": remaining_after,
        "budget_initial_usd": budget_limit_usd,
        "repeat_counts": {tool_name: effective_count},
        "last_tool_name": tool_name,
        "seen_nonces": [],
        "circuit_tripped": False,
    }
    call = build_call(tool_name, parameters)
    return decide(config, state, call)
