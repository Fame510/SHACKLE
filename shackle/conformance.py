"""
SHACKLE — Conformance Reference Decision Function.
Copyright (C) 2026  Dante Bullock, Sovereign Logic.  AGPL-3.0-or-later.

Pure, side-effect-free reference implementation of the SHACKLE SP/1.0
decision surface. Given (config, state, call) it returns a typed verdict
and reason. This is the canonical target that fixtures/conformance.json
encodes; any runtime is SHACKLE-conformant iff it reproduces these verdicts.

Author: Dante Bullock (@Fame510) — sole author.
This module is independent of the runtime shim in shackle/core.py and has
no external dependencies (stdlib only), so it can be executed anywhere.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Tuple

Verdict = str  # "ALLOW" | "DENY" | "HITL"


def canonical_hash(params: Dict[str, Any]) -> str:
    """SHA-256 over canonical JSON: keys sorted, tight separators, UTF-8.

    Mirrors fixtures/conformance.json 'canonicalization'. Non-canonicalizable
    input (NaN/Infinity, non-string keys) MUST be rejected upstream; json.dumps
    with allow_nan=False raises on NaN/Infinity, which callers treat as
    policy_violation:malformed_input.
    """
    serialized = json.dumps(
        params, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def decide(
    config: Dict[str, Any],
    state: Dict[str, Any],
    call: Dict[str, Any],
) -> Tuple[Verdict, str]:
    """Return (verdict, reason) for a single tool call.

    Precedence (highest first) — fail-closed by construction:
      1. malformed / non-canonicalizable input        -> DENY
      2. circuit already open                          -> DENY
      3. duplicate nonce (replay)                      -> DENY
         (specialized: duplicate resume vs terminal    -> DENY)
      4. HITL transition contract (pending_transition) -> ALLOW/DENY/HITL
      5. budget exhausted                              -> DENY
      6. max repeat exceeded                           -> DENY
      7. HITL mode 'always'                            -> HITL
      8. HITL budget threshold                         -> HITL
      9. opaque / untestable context                   -> HITL (fail-closed)
     10. default                                       -> ALLOW
    """
    params: Dict[str, Any] = call.get("params", {}) or {}
    pending = state.get("pending_transition")

    # 1. malformed / non-canonicalizable input
    if params.get("__noncanonical__") is True:
        return ("DENY", "policy_violation:malformed_input")

    # 2. circuit already open
    if state.get("circuit_tripped") is True:
        return ("DENY", "circuit_open")

    # 3. duplicate nonce (replay)
    seen = state.get("seen_nonces") or []
    nonce = call.get("nonce")
    if nonce is not None and nonce in seen:
        if (
            pending
            and pending.get("resume_attempt") is True
            and pending.get("terminal_status") in ("rejected", "superseded")
        ):
            return ("DENY", "policy_violation:duplicate_resume_no_effect")
        return ("DENY", "policy_violation:duplicate_nonce")

    # 4. HITL transition contract
    if pending:
        decision = pending.get("decision")
        if decision == "approve":
            # Released once, bound to original args digest; a differing digest
            # at execution MUST fail closed (enforced by the executor).
            return ("ALLOW", "hitl_transition:approve")
        if decision == "reject":
            return ("DENY", "hitl_transition:reject")
        if decision == "modify":
            # Original is superseded/non-dispatchable; only the edited
            # successor (this preimage) is executable.
            return ("ALLOW", "hitl_transition:modify_successor")
        if decision in ("defer", "escalate"):
            return ("HITL", "hitl_transition:defer_escalate")

    # 5. budget exhausted
    budget = config.get("budget_usd", 0) or 0
    remaining = state.get("budget_remaining_usd")
    if remaining is not None and remaining <= 0 and budget > 0:
        return ("DENY", "budget_exhausted")

    # 6. max repeat exceeded
    max_repeat = config.get("max_repeat_calls")
    repeat_counts = state.get("repeat_counts")
    last_tool = state.get("last_tool_name")
    if max_repeat is not None and repeat_counts and last_tool:
        rc = repeat_counts.get(call.get("tool_name"))
        if rc is not None and rc >= max_repeat and last_tool == call.get("tool_name"):
            return ("DENY", "max_repeat_exceeded")

    # 7. HITL always
    if config.get("hitl_mode") == "always":
        return ("HITL", "hitl_all_calls")

    # 8. HITL budget threshold
    if config.get("hitl_mode") == "on_threshold" and config.get("hitl_budget_threshold") is not None:
        initial = state.get("budget_initial_usd")
        if initial:
            frac = state.get("budget_remaining_usd", 0) / initial
            if frac <= config["hitl_budget_threshold"]:
                return ("HITL", "budget_threshold")

    # 9. opaque / untestable context
    if params.get("ctx") == "opaque":
        return ("HITL", "fail_closed:opaque_context")

    # 10. default allow
    return ("ALLOW", "within_thresholds")
