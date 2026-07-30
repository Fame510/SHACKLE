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

Revision SP/1.0.1 (see SPEC_REVISION) is a strict tightening of SP/1.0:
  * non-canonicalizable input is detected structurally (NaN/Infinity,
    non-string keys, unserializable types) instead of via a magic sentinel;
  * opaque context is detected across the whole context-key class instead of
    the single literal pair {"ctx": "opaque"};
  * the HITL transition contract validates original_nonce /
    original_args_digest / successor_nonce / successor_args_digest /
    terminal_status and fails closed on any mismatch, unknown decision, or
    unbound authorization.
All 15 published SP/1.0 vectors keep their existing canonical_hash,
expected_verdict and expected_reason under this revision. SP/1.0.1 only adds
DENY outcomes to inputs that previously fell through to ALLOW.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, Optional, Tuple

Verdict = str  # "ALLOW" | "DENY" | "HITL"

SPEC_VERSION = "SP/1.0"
SPEC_REVISION = "SP/1.0.1"

# Guard against pathological nesting when walking untrusted params.
_MAX_DEPTH = 64

# Reserved marker used by the language-neutral fixture format to denote the
# class of input JSON cannot literally encode (NaN/Infinity, non-string keys).
_NONCANONICAL_SENTINEL = "__noncanonical__"

# Keys whose value carries agent "context" the guard must be able to evaluate.
# Identical to v2/spec/decide.py::_OPAQUE_CONTEXT_KEYS so the two in-repo
# implementations agree on the class, not merely on the fixture.
_CONTEXT_KEYS = frozenset({"ctx", "context", "opaque", "raw_context", "blob"})

# Value markers that explicitly declare context as non-evaluable.
_OPAQUE_MARKERS = frozenset({"opaque", "unknown", "unevaluable", "untestable"})

# Human decisions the transition contract recognizes. Anything else fails closed.
_TRANSITION_DECISIONS = frozenset({"approve", "reject", "modify", "defer", "escalate"})

# Transition states from which no further execution may be released.
_TERMINAL_STATUSES = frozenset(
    {"rejected", "superseded", "consumed", "executed", "expired", "revoked", "cancelled"}
)


def canonical_hash(params: Dict[str, Any]) -> str:
    """SHA-256 over canonical JSON: keys sorted, tight separators, UTF-8.

    Mirrors fixtures/conformance.json 'canonicalization'. This is the ARGUMENT
    digest — it covers call.params only. To seal a whole conformance vector
    (config + state + call + expected outputs) use vector_hash().
    """
    serialized = json.dumps(
        params, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def vector_hash(fixture: Dict[str, Any]) -> str:
    """SHA-256 over an ENTIRE conformance vector, excluding vector_hash itself.

    canonical_hash pins only the input preimage (call.params), so expected_verdict
    and expected_reason could previously be edited with every published hash still
    verifying. vector_hash seals name/config/state/call/canonical_hash/
    expected_verdict/expected_reason/conformance_note as one unit, using the same
    canonicalization discipline (sorted keys, tight separators, UTF-8, no NaN).

    Added in SP/1.0.1 as a NEW field. canonical_hash keeps its SP/1.0 meaning
    (argument digest) and its published values are unchanged.
    """
    sealed = {k: v for k, v in fixture.items() if k != "vector_hash"}
    serialized = json.dumps(
        sealed, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ──────────────────────────────────────────────────────────────────────
# Canonicalizability (SP/1.0.1 — replaces the __noncanonical__ literal test)
# ──────────────────────────────────────────────────────────────────────

def _walk_canonicalizable(value: Any, depth: int) -> Optional[str]:
    """Depth-first structural check. Returns a diagnostic token or None."""
    if depth > _MAX_DEPTH:
        return "max_depth_exceeded"
    if value is None or isinstance(value, (bool, int, str)):
        return None
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return "non_finite_number"
        return None
    if isinstance(value, dict):
        for k, v in value.items():
            # json.dumps silently coerces int/float/bool/None keys to strings,
            # so two logically-distinct params can collide on one digest. The
            # spec requires string keys; anything else is rejected outright.
            if not isinstance(k, str):
                return "non_string_key"
            err = _walk_canonicalizable(v, depth + 1)
            if err is not None:
                return err
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            err = _walk_canonicalizable(item, depth + 1)
            if err is not None:
                return err
        return None
    return "unsupported_type:" + type(value).__name__


def canonicalization_error(params: Any) -> Optional[str]:
    """Return a diagnostic token if params cannot be canonicalized, else None.

    Rejects, per fixtures/README.md: non-object params, non-string keys,
    NaN/Infinity, types JSON cannot represent, and the reserved
    __noncanonical__ marker the language-neutral fixtures use to stand in for
    the classes JSON itself cannot encode.

    The final json.dumps attempt is deliberate: it is the same call
    canonical_hash() makes, so anything canonical_hash() would raise on is
    reported here as a policy violation instead of escaping as an exception.
    """
    if not isinstance(params, dict):
        return "not_an_object"
    if params.get(_NONCANONICAL_SENTINEL) is True:
        return "declared_non_canonicalizable"
    err = _walk_canonicalizable(params, 0)
    if err is not None:
        return err
    try:
        json.dumps(
            params, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        return "unserializable:" + type(exc).__name__
    return None


def is_canonicalizable(params: Any) -> bool:
    """True iff params can be deterministically canonicalized per SP/1.0."""
    return canonicalization_error(params) is None


# ──────────────────────────────────────────────────────────────────────
# Opaque context (SP/1.0.1 — replaces the {"ctx": "opaque"} literal test)
# ──────────────────────────────────────────────────────────────────────

def _is_opaque_value(value: Any) -> bool:
    """True when a context value cannot be deterministically evaluated."""
    if isinstance(value, str):
        return value.strip().lower() in _OPAQUE_MARKERS
    if value is None or isinstance(value, (bool, int, float, dict, list, tuple)):
        return False
    # Anything the guard cannot introspect (handles, callables, raw buffers)
    # is by definition not deterministically evaluable.
    return True


def opaque_context_key(params: Any, _depth: int = 0) -> Optional[str]:
    """Return the dotted path of the first opaque context binding, else None.

    A binding is opaque when a context-bearing key (ctx / context / opaque /
    raw_context / blob) carries a value the guard cannot evaluate — either an
    explicit non-evaluable marker ("opaque", "unknown", "unevaluable",
    "untestable") or a value it cannot introspect at all.

    The check is a key-class x value-class product, not a literal pair, so
    {"context": "opaque"}, {"ctx": "unknown"}, {"raw_context": "opaque"} and
    {"blob": "unevaluable"} all fail closed — while an ordinary evaluable
    binding such as {"context": "user asked about pricing"} does not.
    """
    if _depth > _MAX_DEPTH or not isinstance(params, dict):
        return None
    for key, value in params.items():
        if not isinstance(key, str):
            continue
        if key.strip().lower() in _CONTEXT_KEYS and _is_opaque_value(value):
            return key
        if isinstance(value, dict):
            nested = opaque_context_key(value, _depth + 1)
            if nested is not None:
                return f"{key}.{nested}"
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                nested = opaque_context_key(item, _depth + 1)
                if nested is not None:
                    return f"{key}[{index}].{nested}"
    return None


def has_opaque_context(params: Any) -> bool:
    """True iff any context binding in params is not deterministically evaluable."""
    return opaque_context_key(params) is not None


# ──────────────────────────────────────────────────────────────────────
# HITL transition contract (SP/1.0.1 — now actually enforced)
# ──────────────────────────────────────────────────────────────────────

def evaluate_transition(
    pending: Dict[str, Any],
    call: Dict[str, Any],
    params_digest: str,
) -> Tuple[Verdict, str]:
    """Resolve a pending human transition against the call being dispatched.

    SP/1.0 encoded original_nonce, original_args_digest, successor_nonce,
    successor_args_digest and terminal_status in the fixtures but the reference
    core read only pending["decision"], so the advertised invariant
    ``history_visible != runtime_executable`` did not hold: the superseded
    original, a tampered args digest and an unrelated nonce all rode an
    ALLOW. SP/1.0.1 binds the release to exactly the authorized (nonce, digest)
    pair and fails closed on everything else.

    An approval is a single-use capability over one specific preimage. It is
    NOT a general permission to call the tool.
    """
    raw_decision = pending.get("decision")
    decision = raw_decision.strip().lower() if isinstance(raw_decision, str) else ""
    if decision not in _TRANSITION_DECISIONS:
        # Forward-compatibility is not a licence to execute: an authority
        # decision this implementation does not understand must not fall
        # through to the default ALLOW.
        return ("DENY", "hitl_transition:unknown_decision")

    raw_status = pending.get("terminal_status")
    status = raw_status.strip().lower() if isinstance(raw_status, str) else ""
    nonce = call.get("nonce")

    # Re-dispatch against an already-terminal transition is no-effect, whether
    # or not the replayed nonce happens to have been recorded in seen_nonces.
    if status in _TERMINAL_STATUSES and pending.get("resume_attempt") is True:
        return ("DENY", "policy_violation:duplicate_resume_no_effect")

    if decision == "reject":
        return ("DENY", "hitl_transition:reject")
    if decision in ("defer", "escalate"):
        return ("HITL", "hitl_transition:defer_escalate")

    if decision == "modify":
        # terminal_status on a MODIFY describes the ORIGINAL (superseded), not
        # the successor, so it is not a terminality bar on the successor here.
        original_nonce = pending.get("original_nonce")
        if original_nonce is not None and nonce == original_nonce:
            return ("DENY", "hitl_transition:superseded_original")
        authorized_nonce = pending.get("successor_nonce")
        authorized_digest = pending.get("successor_args_digest")
        allow_reason = "hitl_transition:modify_successor"
    else:  # approve
        if status in _TERMINAL_STATUSES:
            return ("DENY", "hitl_transition:terminal_no_effect")
        authorized_nonce = pending.get("original_nonce")
        authorized_digest = pending.get("original_args_digest")
        allow_reason = "hitl_transition:approve"

    # An authorization that binds neither a nonce nor an args digest is a blank
    # cheque over the whole tool surface. Refuse to honour it.
    if authorized_nonce is None and authorized_digest is None:
        return ("DENY", "hitl_transition:unbound_authorization")
    if authorized_nonce is not None and nonce != authorized_nonce:
        return ("DENY", "hitl_transition:nonce_mismatch")
    if authorized_digest is not None and params_digest != authorized_digest:
        return ("DENY", "hitl_transition:digest_mismatch")
    return ("ALLOW", allow_reason)


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
         bound to (authorized nonce, authorized args digest); mismatch,
         superseded original, terminal re-dispatch, unknown decision and
         unbound authorization all -> DENY
      5. budget exhausted                              -> DENY
      5b. budget overrun (this call would push remaining negative) -> DENY
      6. max repeat exceeded                           -> DENY
      7. HITL mode 'always'                            -> HITL
      8. HITL budget threshold                         -> HITL
      9. opaque / untestable context                   -> HITL (fail-closed)
     10. default                                       -> ALLOW

    Note on precedence 4 vs 5: a human approval is evaluated BEFORE budget.
    That is deliberate and unchanged in SP/1.0.1 — an explicit, digest-bound,
    single-use human release outranks the automated budget ceiling — but it is
    a policy choice, not an accident. Deployments that require budget to be
    absolute must not populate pending_transition once remaining reaches zero.
    """
    params: Dict[str, Any] = call.get("params", {}) or {}
    pending = state.get("pending_transition")

    # 1. malformed / non-canonicalizable input
    if canonicalization_error(params) is not None:
        return ("DENY", "policy_violation:malformed_input")

    # 2. circuit already open
    if state.get("circuit_tripped") is True:
        return ("DENY", "circuit_open")

    # 3. duplicate nonce (replay)
    seen = state.get("seen_nonces") or []
    nonce = call.get("nonce")
    if nonce is not None and nonce in seen:
        # Re-dispatch of a nonce belonging to an already-terminal transition is
        # a duplicate RESUME, which is a strictly more specific diagnosis than a
        # generic replay. SP/1.0 additionally required pending["resume_attempt"]
        # is True — but that flag is advisory metadata supplied by the caller,
        # so omitting it changed the reported reason. The reason string is
        # normative (SP-1.0-SPECIFICATION.md §3.5), so it must not be steerable
        # by the party being judged. Terminality alone decides it now.
        if isinstance(pending, dict):
            raw_status = pending.get("terminal_status")
            status = raw_status.strip().lower() if isinstance(raw_status, str) else ""
            if status in _TERMINAL_STATUSES:
                return ("DENY", "policy_violation:duplicate_resume_no_effect")
        return ("DENY", "policy_violation:duplicate_nonce")

    # 4. HITL transition contract. params are canonicalizable by rule 1, so the
    #    digest is computed with the same function the fixtures publish.
    if isinstance(pending, dict) and pending:
        return evaluate_transition(pending, call, canonical_hash(params))

    # 5. budget exhausted
    budget = config.get("budget_usd", 0) or 0
    remaining = state.get("budget_remaining_usd")
    if remaining is not None and remaining <= 0 and budget > 0:
        return ("DENY", "budget_exhausted")

    # 5b. budget overrun: this single call's estimated cost would push
    # remaining negative. Distinct from budget_exhausted: there is budget
    # left, but not enough for this call. This is the contract that pins
    # "fail closed under concurrency" -- if two threads each see remaining
    # > 0 and each try a call whose cost > remaining, the first to land
    # must NOT silently drain the budget past zero on the second. The
    # runtime MUST consult decide() with the pre-call remaining and the
    # call's estimated_cost_usd so this check fires BEFORE mutation.
    estimated = call.get("estimated_cost_usd", 0) or 0
    if (remaining is not None
            and budget > 0
            and estimated > 0
            and (remaining - estimated) < 0):
        return ("DENY", "budget_overrun")

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
    if has_opaque_context(params):
        return ("HITL", "fail_closed:opaque_context")

    # 10. default allow
    return ("ALLOW", "within_thresholds")
