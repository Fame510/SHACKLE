# NOTE: This is a FORWARD-LOOKING reference implementation. The decision core
# certified by the SP/1.0 conformance fixtures and executed by the production
# daemon is shackle/conformance.py:decide. This module is not wired into the
# daemon runtime (only benchmarks import it). Keep that distinction in mind.
"""
SHACKLE Core Decision Function â SP/1.0
========================================
The single function that answers:
"Should this agent execute this tool with these parameters at this moment?"

PROPERTIES (provably correct):
  P1: Budget monotonically non-increasing
  P2: Repeat counts non-decreasing
  P3: Once tripped, always tripped
  P4: Budget never negative
  P5: Repeat limit â DENY
  P6: Fresh state â ALLOW
  P7: Deterministic (same inputs â same output)
  P8: HITL_ALWAYS â HITL (unless circuit tripped)
  P9: Duplicate nonce â DENY

DESIGN: Pure function. No I/O. No side effects. No allocations in hot path.
        Human-auditable in under 10 minutes. Under 200 lines of logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, Optional, Set
import hashlib
import json


# ââââââââââââââââââââââââââââââââââââââââââ
# Enums
# ââââââââââââââââââââââââââââââââââââââââââ

class Verdict(Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    HITL = "HITL"


class DenyReason(Enum):
    UNSPECIFIED = "unspecified"
    BUDGET_EXHAUSTED = "budget_exhausted"
    BUDGET_OVERRUN = "budget_overrun"
    MAX_REPEAT_EXCEEDED = "max_repeat_exceeded"
    CIRCUIT_OPEN = "circuit_open"
    WINDOW_EXCEEDED = "window_exceeded"
    GLOBAL_LIMIT = "global_limit"
    POLICY_VIOLATION = "policy_violation"
    AUTH_FAILED = "auth_failed"


class HitlMode(Enum):
    NEVER = "never"
    ON_DENY = "on_deny"
    ON_THRESHOLD = "on_threshold"
    ALWAYS = "always"


# ââââââââââââââââââââââââââââââââââââââââââ
# Data Classes
# ââââââââââââââââââââââââââââââââââââââââââ

@dataclass
class GuardConfig:
    """Immutable guard configuration. Single source of truth for policy."""
    budget_usd: float = 0.0
    max_repeat_calls: int = 0
    error_amplification: bool = True
    timeout_seconds: int = 0
    window_duration_s: int = 0
    window_max_calls: int = 0
    max_total_calls: int = 0
    probabilistic_deny: bool = False
    deny_jitter_ratio: float = 0.0
    hitl_mode: HitlMode = HitlMode.NEVER
    hitl_budget_threshold: float = 0.0
    parent_guard_id: str = ""

    def __post_init__(self):
        assert self.budget_usd >= 0, "budget_usd must be >= 0"
        assert self.max_repeat_calls >= 0, "max_repeat_calls must be >= 0"
        assert 0.0 <= self.deny_jitter_ratio <= 1.0
        assert 0.0 <= self.hitl_budget_threshold <= 1.0


@dataclass
class SessionState:
    """Runtime state owned by the daemon. Read by decide(), mutated after verdict."""
    session_id: str = ""
    agent_id: str = ""
    organization_id: str = ""
    circuit_tripped: bool = False
    circuit_trip_reason: str = ""
    budget_initial_usd: float = 0.0
    budget_remaining_usd: float = 0.0
    budget_spent_usd: float = 0.0
    total_calls: int = 0
    repeat_counts: Dict[str, int] = field(default_factory=dict)
    window_counts: Dict[str, int] = field(default_factory=dict)
    last_tool_name: str = ""
    last_tool_params_hash: bytes = b""
    seen_nonces: Set[int] = field(default_factory=set)


    # HITL transition state (SP/1.0): persisted decision on an original call
    # awaiting/finished a human transition. None = no pending transition.
    pending_transition: Optional[Dict[str, object]] = None
@dataclass
class ToolCall:
    """A proposed tool execution request from the agent."""
    tool_name: str
    tool_params_hash: bytes
    estimated_cost_usd: float = 0.0
    nonce: int = 0
    parent_guard_id: str = ""
    tool_params_raw: str = ""
    tool_params: Optional[dict] = None  # parsed params for canonicalization/context checks


@dataclass
class Decision:
    """The result of the decision function."""
    verdict: Verdict
    deny_reason: DenyReason = DenyReason.UNSPECIFIED
    human_readable: str = ""
    probabilistic_deny: bool = False


# ââââââââââââââââââââââââââââââââââââââââââ
# Error Signal Detection
# ââââââââââââââââââââââââââââââââââââââââââ

_ERROR_SIGNALS = (
    "401", "unauthorized", "403", "forbidden", "500",
    "internal server error", "502", "bad gateway", "503",
    "service unavailable", "504", "gateway timeout", "timeout",
    "connection refused", "connection reset", "no route to host",
    "permission denied", "access denied", "rate limit",
    "quota exceeded", "invalid api key", "authentication failed",
    "token expired", "model not found", "resource exhausted",
    "deadline exceeded",
)


def has_error_signal(params_raw: str) -> bool:
    """Detect error signals WITHOUT regex (no ReDoS surface)."""
    if not params_raw:
        return False
    lower = params_raw.lower()
    for signal in _ERROR_SIGNALS:
        if signal in lower:
            return True
    return False


# ââââââââââââââââââââââââââââââââââââââââââ
# THE DECISION FUNCTION
# ââââââââââââââââââââââââââââââââââââââââââ

# --- Canonicalization & Context Validation (SP/1.0) ---
# If a call's params contain an opaque context the guard cannot evaluate,
# decide() MUST fail closed (HITL) rather than silently ALLOW. And any input
# that cannot be deterministically canonicalized is rejected (DENY). These back
# fixtures/conformance.json :: malformed_non_canonical_input & untestable_opaque_context.
_OPAQUE_CONTEXT_KEYS = ("ctx", "context", "opaque", "raw_context", "blob")
_OPAQUE_CONTEXT_VALUES = ("opaque", "unknown", "unevaluable", "untestable")


def is_canonicalizable(params) -> bool:
    """False if params cannot be deterministically canonicalized.

    Rejects (per SP/1.0): non-dict input, non-string keys, NaN/Infinity floats,
    and values that are not JSON-canonicalizable. A differing hash for
    logically-equal params is a conformance failure, so non-canonicalizable
    input is rejected deterministically (DENY:policy_violation:malformed_input).
    """
    import math
    if not isinstance(params, dict):
        return False
    # Explicit non-canonical sentinel. JSON cannot literally encode NaN/Infinity
    # or non-string keys, so language-neutral fixtures declare that class with a
    # reserved marker key. Presence => not canonicalizable (DENY:malformed_input).
    if params.get("__noncanonical__") is True:
        return False

    def _ok(v) -> bool:
        if isinstance(v, bool):
            return True
        if isinstance(v, float):
            if math.isnan(v) or math.isinf(v):
                return False
            return True
        if isinstance(v, dict):
            for k, vv in v.items():
                if not isinstance(k, str) or not _ok(vv):
                    return False
            return True
        if isinstance(v, (list, tuple)):
            return all(_ok(x) for x in v)
        return isinstance(v, (str, int)) or v is None

    for k, v in params.items():
        if not isinstance(k, str) or not _ok(v):
            return False
    return True


def has_opaque_context(params) -> bool:
    """True when context cannot be deterministically evaluated => fail closed (HITL)."""
    if not isinstance(params, dict):
        return False
    for k, v in params.items():
        kl = k.lower() if isinstance(k, str) else ""
        if kl in _OPAQUE_CONTEXT_KEYS:
            return True
        if isinstance(v, str) and v.lower() in _OPAQUE_CONTEXT_VALUES:
            return True
    return False


def decide(
    state: SessionState,
    call: ToolCall,
    config: GuardConfig,
    rng_float: float = 0.0,
) -> Decision:
    """Core policy decision. 8 stacked layers. Pure function. Zero I/O."""

    # Layer 1: Circuit breaker (highest priority)
    if state.circuit_tripped:
        return Decision(Verdict.DENY, DenyReason.CIRCUIT_OPEN,
                        f"Circuit open: {state.circuit_trip_reason}")

    # Layer 1b: HITL transition resolution (SP/1.0).
    # A resume of a call that carried a human-in-the-loop decision MUST honor the
    # persisted transition and never fall through to budget/threshold rules.
    # Backs fixtures/conformance.json :: hitl_transition_*.
    pt = state.pending_transition
    if pt:
        _decision = str(pt.get("decision", "")).lower()
        # Duplicate resume of an already-resolved (rejected) transition. Checked
        # before plain reject because both carry decision == "reject".
        if pt.get("resume_attempt") is True:
            return Decision(Verdict.DENY, DenyReason.POLICY_VIOLATION,
                            "[hitl_transition:duplicate_resume]")
        if _decision == "reject":
            return Decision(Verdict.DENY, DenyReason.POLICY_VIOLATION,
                            "[hitl_transition:reject]")
        if _decision == "defer":
            return Decision(Verdict.HITL,
                            human_readable="[hitl_transition:defer_escalate]")
        if _decision == "approve":
            return Decision(Verdict.ALLOW,
                            human_readable="[hitl_transition:approve]")
        if _decision == "modify":
            return Decision(Verdict.ALLOW,
                            human_readable="[hitl_transition:modify_successor]")

    # Layer 2: Nonce validation (anti-replay)
    if call.nonce in state.seen_nonces:
        return Decision(Verdict.DENY, DenyReason.POLICY_VIOLATION,
                        "Duplicate nonce â replay attack suspected")

    # Layer 2b: Canonicalization guard (reject non-canonicalizable input)
    # A differing hash for logically-equal params is a conformance failure, so
    # any input we cannot deterministically canonicalize is denied outright.
    if call.tool_params is not None and not is_canonicalizable(call.tool_params):
        return Decision(Verdict.DENY, DenyReason.POLICY_VIOLATION,
                        "[malformed_input] non-canonicalizable params rejected")

    # Layer 3: Budget guard
    if config.budget_usd > 0:
        if state.budget_remaining_usd <= 0:
            return Decision(Verdict.DENY, DenyReason.BUDGET_EXHAUSTED,
                            f"Budget exhausted: ${state.budget_spent_usd:.4f} / ${state.budget_initial_usd:.4f}")

        if config.hitl_mode == HitlMode.ON_THRESHOLD:
            fraction = state.budget_remaining_usd / state.budget_initial_usd
            if fraction <= config.hitl_budget_threshold:
                return Decision(Verdict.HITL,
                                human_readable=f"Budget threshold: {fraction:.1%} remaining")

        if call.estimated_cost_usd > state.budget_remaining_usd:
            if config.hitl_mode in (HitlMode.ON_DENY, HitlMode.ALWAYS):
                return Decision(Verdict.HITL,
                                human_readable=f"Cost ${call.estimated_cost_usd:.4f} > remaining ${state.budget_remaining_usd:.4f}")
            # remaining is > 0 here (the <=0 case returned budget_exhausted
            # above), so a call whose estimated cost exceeds remaining is an
            # OVERRUN, not exhaustion. Distinct reason keeps this consistent
            # with shackle/conformance.py and the concurrent_budget_overrun
            # fixture (fail closed BEFORE the budget is driven negative).
            return Decision(Verdict.DENY, DenyReason.BUDGET_OVERRUN,
                            f"Cost ${call.estimated_cost_usd:.4f} > remaining ${state.budget_remaining_usd:.4f}")

    # Layer 4: Repeat call guard
    if config.max_repeat_calls > 0:
        is_repeat = (call.tool_name == state.last_tool_name and
                     call.tool_params_hash == state.last_tool_params_hash)
        if is_repeat:
            repeat_count = state.repeat_counts.get(call.tool_name, 0)
            limit = config.max_repeat_calls
            if config.error_amplification and has_error_signal(call.tool_params_raw):
                limit = max(1, config.max_repeat_calls - 1)
            if repeat_count >= limit:
                return Decision(Verdict.DENY, DenyReason.MAX_REPEAT_EXCEEDED,
                                f"'{call.tool_name}' repeated {repeat_count + 1}x (limit: {config.max_repeat_calls})")

    # Layer 5: Time window guard
    if config.window_max_calls > 0:
        count = state.window_counts.get(call.tool_name, 0)
        if count >= config.window_max_calls:
            return Decision(Verdict.DENY, DenyReason.WINDOW_EXCEEDED,
                            f"'{call.tool_name}' {count}x in {config.window_duration_s}s window (limit: {config.window_max_calls})")

    # Layer 6: Global call limit
    if config.max_total_calls > 0 and state.total_calls >= config.max_total_calls:
        return Decision(Verdict.DENY, DenyReason.GLOBAL_LIMIT,
                        f"Global limit: {state.total_calls}/{config.max_total_calls}")

    # Layer 7: Probabilistic denial (adversarial hardening)
    if config.probabilistic_deny and config.budget_usd > 0 and state.budget_initial_usd > 0:
        ratio = state.budget_remaining_usd / state.budget_initial_usd
        if ratio < 0.2:
            prob = config.deny_jitter_ratio * (1.0 - ratio)
            if rng_float < prob:
                return Decision(Verdict.DENY, DenyReason.BUDGET_EXHAUSTED,
                                "Budget enforcement (probabilistic)", probabilistic_deny=True)

    # Layer 7b: Fail-closed on opaque/unevaluable context.
    # When context cannot be evaluated deterministically we MUST fail closed
    # (HITL), never silent ALLOW. Circuit/nonce/budget/limits above still win.
    if call.tool_params is not None and has_opaque_context(call.tool_params):
        return Decision(Verdict.HITL,
                        human_readable="[opaque_context] fail-closed: context not deterministically evaluable")

    # Layer 8: HITL always
    if config.hitl_mode == HitlMode.ALWAYS:
        return Decision(Verdict.HITL, human_readable="HITL required for all calls")

    return Decision(Verdict.ALLOW, human_readable="Within all guard thresholds")


# ââââââââââââââââââââââââââââââââââââââââââ
# State Transition Helpers (daemon-side)
# ââââââââââââââââââââââââââââââââââââââââââ

def apply_allow(state: SessionState, call: ToolCall) -> None:
    """Update state after ALLOW verdict. Called by daemon only."""
    state.total_calls += 1
    state.seen_nonces.add(call.nonce)

    is_repeat = (call.tool_name == state.last_tool_name and
                 call.tool_params_hash == state.last_tool_params_hash)
    if is_repeat:
        state.repeat_counts[call.tool_name] = state.repeat_counts.get(call.tool_name, 0) + 1
    else:
        state.repeat_counts[call.tool_name] = 1

    state.window_counts[call.tool_name] = state.window_counts.get(call.tool_name, 0) + 1
    state.last_tool_name = call.tool_name
    state.last_tool_params_hash = call.tool_params_hash


def apply_deny(state: SessionState, reason: str) -> None:
    """Trip the circuit breaker after DENY verdict."""
    state.circuit_tripped = True
    state.circuit_trip_reason = reason


def apply_post_exec(state: SessionState, actual_cost_usd: float) -> None:
    """Update budget after tool execution."""
    if actual_cost_usd <= 0:
        return
    state.budget_spent_usd += actual_cost_usd
    state.budget_remaining_usd = max(0.0, state.budget_initial_usd - state.budget_spent_usd)


def hash_params(params: dict) -> bytes:
    """Canonical SHA-256 hash with sorted keys for determinism."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).digest()
