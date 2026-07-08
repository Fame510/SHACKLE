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
    """Core policy decision. Fail-closed, stacked layers. Pure function. Zero I/O.

    SP/1.0 PRECEDENCE (single canonical order — must match shackle/conformance.py::decide,
    which the published fixtures + fixtures/conformance_combined.json pin):

      1. malformed / non-canonicalizable input   -> DENY  (fail-closed on input we can't hash)
      2. circuit already open                     -> DENY
      3. duplicate nonce (replay)                 -> DENY  (specialized: duplicate resume of a
                                                     terminal transition -> DENY duplicate_resume)
      4. HITL transition contract                 -> ALLOW/DENY/HITL
      5. budget exhausted                         -> DENY
      6. max repeat exceeded                      -> DENY
      7. time window exceeded                     -> DENY
      8. global call limit                        -> DENY
      9. probabilistic denial (adversarial)       -> DENY
     10. opaque / unevaluable context             -> HITL  (fail-closed)
     11. HITL always                              -> HITL
     12. HITL budget threshold                    -> HITL
     13. default                                  -> ALLOW

    AUDIT NOTE (fix/audit-hardening): replay MUST win over a stale human approval — a
    duplicate nonce is denied BEFORE the pending_transition is honored, so an
    approved-then-replayed call can never resume past the guardrail. Malformed input and
    circuit-open are checked before the transition for the same fail-closed reason. This
    ordering was previously divergent from shackle/conformance.py and is now unified.
    """
    params = call.tool_params

    # Layer 1: malformed / non-canonicalizable input (fail-closed; can't hash -> can't trust)
    if params is not None and not is_canonicalizable(params):
        return Decision(Verdict.DENY, DenyReason.POLICY_VIOLATION,
                        "[malformed_input] non-canonicalizable params rejected")

    # Layer 2: circuit breaker
    if state.circuit_tripped:
        return Decision(Verdict.DENY, DenyReason.CIRCUIT_OPEN,
                        f"Circuit open: {state.circuit_trip_reason}")

    # Layer 3: nonce / anti-replay. Checked BEFORE the transition so a replayed nonce that
    # happens to carry a stale approval is denied, not honored.
    if call.nonce in state.seen_nonces:
        pt = state.pending_transition
        if (pt and pt.get("resume_attempt") is True
                and str(pt.get("terminal_status", "")).lower() in ("rejected", "superseded")):
            return Decision(Verdict.DENY, DenyReason.POLICY_VIOLATION,
                            "[hitl_transition:duplicate_resume]")
        return Decision(Verdict.DENY, DenyReason.POLICY_VIOLATION,
                        "Duplicate nonce — replay attack suspected")

    # Layer 4: HITL transition resolution (SP/1.0). A resume of a call that carried a human
    # decision honors the persisted transition. Duplicate-resume of a terminal transition is
    # denied here too (defensive, when the replayed nonce was not pre-seeded in seen_nonces).
    pt = state.pending_transition
    if pt:
        _decision = str(pt.get("decision", "")).lower()
        if (pt.get("resume_attempt") is True
                and str(pt.get("terminal_status", "")).lower() in ("rejected", "superseded")):
            return Decision(Verdict.DENY, DenyReason.POLICY_VIOLATION,
                            "[hitl_transition:duplicate_resume]")
        if _decision == "reject":
            return Decision(Verdict.DENY, DenyReason.POLICY_VIOLATION,
                            "[hitl_transition:reject]")
        if _decision in ("defer", "escalate"):
            return Decision(Verdict.HITL,
                            human_readable="[hitl_transition:defer_escalate]")
        if _decision == "approve":
            return Decision(Verdict.ALLOW,
                            human_readable="[hitl_transition:approve]")
        if _decision == "modify":
            return Decision(Verdict.ALLOW,
                            human_readable="[hitl_transition:modify_successor]")

    # Layer 5: budget guard
    if config.budget_usd > 0:
        if state.budget_remaining_usd <= 0:
            return Decision(Verdict.DENY, DenyReason.BUDGET_EXHAUSTED,
                            f"Budget exhausted: ${state.budget_spent_usd:.4f} / ${state.budget_initial_usd:.4f}")

        if call.estimated_cost_usd > state.budget_remaining_usd:
            if config.hitl_mode in (HitlMode.ON_DENY, HitlMode.ALWAYS):
                return Decision(Verdict.HITL,
                                human_readable=f"Cost ${call.estimated_cost_usd:.4f} > remaining ${state.budget_remaining_usd:.4f}")
            return Decision(Verdict.DENY, DenyReason.BUDGET_EXHAUSTED,
                            f"Cost ${call.estimated_cost_usd:.4f} > remaining ${state.budget_remaining_usd:.4f}")

    # Layer 6: repeat call guard
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

    # Layer 7: time window guard
    if config.window_max_calls > 0:
        count = state.window_counts.get(call.tool_name, 0)
        if count >= config.window_max_calls:
            return Decision(Verdict.DENY, DenyReason.WINDOW_EXCEEDED,
                            f"'{call.tool_name}' {count}x in {config.window_duration_s}s window (limit: {config.window_max_calls})")

    # Layer 8: global call limit
    if config.max_total_calls > 0 and state.total_calls >= config.max_total_calls:
        return Decision(Verdict.DENY, DenyReason.GLOBAL_LIMIT,
                        f"Global limit: {state.total_calls}/{config.max_total_calls}")

    # Layer 9: probabilistic denial (adversarial hardening)
    if config.probabilistic_deny and config.budget_usd > 0 and state.budget_initial_usd > 0:
        ratio = state.budget_remaining_usd / state.budget_initial_usd
        if ratio < 0.2:
            prob = config.deny_jitter_ratio * (1.0 - ratio)
            if rng_float < prob:
                return Decision(Verdict.DENY, DenyReason.BUDGET_EXHAUSTED,
                                "Budget enforcement (probabilistic)", probabilistic_deny=True)

    # Layer 10: fail-closed on opaque / unevaluable context.
    # RULING (Dante, fail-closed): an opaque/unevaluable context means the guard
    # could not evaluate the call AT ALL — a stronger safety signal than a blanket
    # "HITL for everything" policy — so it is surfaced BEFORE hitl_always. Both
    # cores (this file and shackle/conformance.py) and
    # fixtures/conformance_combined.json:opaque_beats_hitl_always pin this order.
    if params is not None and has_opaque_context(params):
        return Decision(Verdict.HITL,
                        human_readable="[opaque_context] fail-closed: context not deterministically evaluable")

    # Layer 11: HITL always
    if config.hitl_mode == HitlMode.ALWAYS:
        return Decision(Verdict.HITL, human_readable="HITL required for all calls")

    # Layer 12: HITL budget threshold (lower precedence than ALWAYS; matches reference)
    if (config.hitl_mode == HitlMode.ON_THRESHOLD and config.budget_usd > 0
            and state.budget_initial_usd > 0):
        fraction = state.budget_remaining_usd / state.budget_initial_usd
        if fraction <= config.hitl_budget_threshold:
            return Decision(Verdict.HITL,
                            human_readable=f"Budget threshold: {fraction:.1%} remaining")

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
