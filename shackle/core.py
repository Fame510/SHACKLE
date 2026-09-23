"""
SHACKLE — Runtime Circuit Breaker for Autonomous AI Agents.
Copyright (C) 2026 Dante Bullock, Sovereign Logic

Intercepts LLM calls and tool executions at the interpreter level via
dynamic runtime patching. No framework modifications required.

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published
by the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
"""
import asyncio
import contextvars
import json
import sys
import time
import threading
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field

# SP/1.0 wiring: the runtime now consults the SAME reference decision function
# that the published conformance fixtures encode. core.py is no longer a
# separate implementation of the decision surface — it maps its live runtime
# state onto decide()'s (config, state, call) contract and honors its verdict.
try:
    from .conformance import (
        decide as _sp_decide,
        canonical_hash as _canonical_hash,
        decide_checked as _decide_checked,
        normalize_decision as _normalize_decision,
    )
except ImportError:  # pragma: no cover - allows running core.py in isolation
    from conformance import (
        decide as _sp_decide,
        canonical_hash as _canonical_hash,
        decide_checked as _decide_checked,
        normalize_decision as _normalize_decision,
    )

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()
logger = logging.getLogger("shackle")

# ──────────────────────────────────────────────
# 1. MODEL PRICING TABLE (per 1M tokens, USD)
# ──────────────────────────────────────────────
MODEL_PRICING: Dict[str, Dict[str, float]] = {
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-2024-05-13": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4-turbo": {"input": 10.00, "output": 30.00},
    "gpt-4": {"input": 30.00, "output": 60.00},
    "claude-3-5-sonnet": {"input": 3.00, "output": 15.00},
    "claude-3-5-haiku": {"input": 0.80, "output": 4.00},
    "claude-3-opus": {"input": 15.00, "output": 75.00},
    "claude-3-sonnet": {"input": 3.00, "output": 15.00},
    "claude-3-haiku": {"input": 0.25, "output": 1.25},
    "gemini-1.5-pro": {"input": 1.25, "output": 5.00},
    "gemini-1.5-flash": {"input": 0.075, "output": 0.30},
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
    "default": {"input": 2.00, "output": 10.00},
}

_PRICING_WARNED: set = set()
_PRICING_DELIMS = ("-", "@", ":", ".")

def _resolve_pricing(model: Any) -> Dict[str, float]:
    """Resolve a model string to a pricing row.

    Exact-match lookup silently priced every provider-prefixed or dated model
    id ("openai/gpt-4", "anthropic/claude-3-5-sonnet-20241022",
    "gpt-4o-2024-08-06") at the *default* row. For gpt-4 that is $2/1M input
    instead of $30/1M: a 15x undercount, so the budget breaker fired 15x late.

    Order: exact -> provider prefix stripped -> longest table key that is a
    delimiter-bounded prefix of the name ("gpt-4o" must not match "gpt-4o-mini",
    and "gpt-4" must not match "gpt-4o"). Unknown models still fall to the
    default row, but loudly, once per model, instead of silently.
    """
    name = str(model or "").strip().lower()
    if name in MODEL_PRICING:
        return MODEL_PRICING[name]
    bare = name.rsplit("/", 1)[-1]
    if bare in MODEL_PRICING:
        return MODEL_PRICING[bare]
    best = None
    for key in MODEL_PRICING:
        if key == "default":
            continue
        if bare.startswith(key) and len(bare) > len(key) and bare[len(key)] in _PRICING_DELIMS:
            if best is None or len(key) > len(best):
                best = key
    if best is not None:
        return MODEL_PRICING[best]
    if name not in _PRICING_WARNED:
        _PRICING_WARNED.add(name)
        logger.warning(
            "SHACKLE: no pricing row for model %r; using the 'default' row. "
            "Budget enforcement for this model is only as accurate as that "
            "row -- add it to MODEL_PRICING.", model)
    return MODEL_PRICING["default"]


@dataclass
class ExecutionState:
    """Live telemetry state tracked entirely in-process."""
    total_cost: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    start_time: float = field(default_factory=time.time)
    total_tool_calls: int = 0
    tool_history: Dict[Tuple[str, str], int] = field(default_factory=dict)
    last_decision: Tuple[str, str] = ("ALLOW", "within_thresholds")  # SP/1.0 decide() verdict
    # SP/1.0 decide() reads state.circuit_tripped and state.seen_nonces. Before
    # SP/1.0.1 the runtime hardcoded both to False/[] when calling decide(), so
    # the circuit-open and replay rules were structurally unreachable from the
    # tool path no matter what the deployment did. They are real fields now.
    circuit_tripped: bool = False
    circuit_trip_reason: str = ""
    seen_nonces: List[Any] = field(default_factory=list)
    # First operator-Abort / hard-deny interrupt, latched. Frameworks (CrewAI
    # flow listeners, agent error handlers) catch and swallow arbitrary
    # exceptions raised from inside a hooked call. Without a latch, an Abort
    # only unwinds one stack frame and the framework keeps calling the LLM and
    # tools. Once set, every later hooked call re-denies and Guard re-raises it
    # at the boundary so the caller sees the REAL trigger.
    pending_interrupt: Optional[Any] = None
    # litellm calls that bypassed the module-attribute patch (early
    # `from litellm import completion`) and are accounted via the success
    # callback instead. Ids are registered by the gate, consumed by the callback.
    bypass_call_ids: set = field(default_factory=set)
    # Per-state RLock guards every read-modify-write of the fields above.
    # The previous design mutated total_cost, input_tokens, output_tokens,
    # total_tool_calls, and tool_history without any synchronization, so
    # two threads could each read the same pre-mutation value, both write
    # their delta, and both pass the post-mutation budget/repeat check
    # (classic lost-update). RLock (not Lock) because a single thread may
    # legitimately re-enter: e.g. an LLM call patched by shackle triggering
    # another LLM call on the same Guard's state. Each Guard scope gets its
    # own state, so distinct Guard instances never contend.
    _lock: "threading.RLock" = field(default_factory=threading.RLock)

    def trip_circuit(self, reason: str) -> None:
        """Latch the circuit open. Every later decide() consultation DENYs."""
        with self._lock:
            if not self.circuit_tripped:
                self.circuit_tripped = True
                self.circuit_trip_reason = reason

    def latch_interrupt(self, si: Any) -> None:
        """Latch the FIRST terminal interrupt; later ones never overwrite it."""
        with self._lock:
            if self.pending_interrupt is None:
                self.pending_interrupt = si
            self.trip_circuit(getattr(si, "trigger_type", "ABORT"))

    def reset_circuit(self) -> None:
        """Clear a latched circuit. Only a human Resume may call this."""
        with self._lock:
            self.circuit_tripped = False
            self.circuit_trip_reason = ""

    def record_nonce(self, nonce: Any) -> None:
        """Record a nonce as consumed so a replay of it is denied."""
        if nonce is None:
            return
        with self._lock:
            if nonce not in self.seen_nonces:
                self.seen_nonces.append(nonce)


class ShackleInterrupt(Exception):
    def __init__(self, message: str, trigger_type: str, state: ExecutionState, details: Dict[str, Any]):
        super().__init__(message)
        self.trigger_type = trigger_type
        self.state = state
        self.details = details


# ──────────────────────────────────────────────
# FIX #1: canonical dedup key for loop-of-death detection
# ──────────────────────────────────────────────
def _canonicalize_tool_input(tool_input: Any) -> str:
    """Canonical string key for loop-of-death dedup.

    The bug this fixes: the previous implementation used plain str(tool_input)
    as the dedup key. Python's dict repr is insertion-order-sensitive, so two
    calls with IDENTICAL content but different key construction order --
    {"query": "x", "error": "y"} vs {"error": "y", "query": "x"} -- hashed to
    different keys and were tracked as unrelated calls, silently defeating
    the loop detector for exactly the payloads (error-bearing retries) it
    most needs to catch.

    Fix: for dict-like/JSON-serializable input, use sort_keys=True canonical
    JSON (the same discipline shackle/conformance.py already uses for
    canonical_hash, just not previously wired into core.py). Falls back to
    str() for inputs that aren't JSON-serializable (already order-independent
    for scalars/strings, so no regression there).
    """
    if isinstance(tool_input, dict):
        try:
            # Same canonical serialization discipline as conformance.canonical_hash.
            return json.dumps(
                tool_input, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, default=str,
            )
        except (TypeError, ValueError):
            pass
    return str(tool_input).strip()


class TriggerEngine:
    def __init__(self, budget: float = 0.20, max_repeat_calls: int = 3,
                 timeout_seconds: float = 180.0, max_tool_calls: int = 50):
        self.budget = budget
        self.max_repeat_calls = max_repeat_calls
        self.timeout_seconds = timeout_seconds
        self.max_tool_calls = max_tool_calls

    def _consult_decide(
        self,
        tool_name: str,
        effective_count: int,
        state: ExecutionState,
        params: Optional[Dict[str, Any]] = None,
        nonce: Any = None,
        estimated_cost_usd: float = 0.0,
    ) -> Tuple[str, str]:
        """Invoke the SP/1.0 reference decide() with runtime-derived inputs.

        Builds the (config, state, call) shape decide() expects from live
        TriggerEngine/ExecutionState values so the runtime and the published
        conformance vectors share ONE decision surface, and returns decide()'s
        (verdict, reason) — which evaluate_tool_call() then ENFORCES.

        Before SP/1.0.1 this method hardcoded ``circuit_tripped: False``,
        ``seen_nonces: []`` and ``params: {}``. Three of decide()'s ten rules
        (circuit_open, duplicate_nonce, malformed_input) and its opaque-context
        rule were therefore unreachable from the tool path by construction: the
        runtime asked the standard a question whose answer it had already
        pre-decided. All four inputs are now live.

        The RESULT is now validated, not just the call. Previously this
        method returned decide()'s return value untouched and the caller tuple-
        unpacked it, so a decision function that returned a non-pair raised
        TypeError/ValueError OUTSIDE any handler, and one that returned an
        unrecognized verdict fell through every DENY/HITL branch downstream and
        the call executed. decide_checked() now guarantees an enforceable
        (verdict, reason) in the SP/1.0 enum for every possible return value
        and every throw, so this method cannot hand the enforcement path
        anything it is unable to act on.
        """
        config = {
            "budget_usd": self.budget,
            "max_repeat_calls": self.max_repeat_calls,
        }
        remaining = max(self.budget - state.total_cost, 0.0)
        decide_state = {
            "circuit_tripped": state.circuit_tripped,
            "seen_nonces": list(state.seen_nonces),
            "budget_initial_usd": self.budget,
            "budget_remaining_usd": remaining,
            "repeat_counts": {tool_name: effective_count},
            "last_tool_name": tool_name,
        }
        call = {
            "tool_name": tool_name,
            "params": params if params is not None else {},
            "nonce": nonce,
            "estimated_cost_usd": estimated_cost_usd,
        }
        verdict, reason = _decide_checked(_sp_decide, config, decide_state, call)
        if reason == "decide_unavailable_fail_closed":
            logger.warning(
                "SHACKLE: decide() raised on the tool path; failing closed.")
        elif reason.startswith("malformed_decision:"):
            logger.warning(
                "SHACKLE: decide() returned an out-of-contract result on the "
                "tool path (%s); failing closed.", reason)
        return (verdict, reason)

    def precheck_llm_call(self, state: "ExecutionState") -> None:
        """Pre-call gate: refuse an LLM call BEFORE it is made when the run is
        already out of time or budget. evaluate_llm_call() is post-call (it
        needs token counts); this closes the gap for paths where we only get to
        intercept the call, not read its response."""
        with state._lock:
            elapsed = time.time() - state.start_time
            if elapsed > self.timeout_seconds:
                raise ShackleInterrupt(
                    message=f"Execution timeout: {elapsed:.1f}s elapsed (limit: {self.timeout_seconds}s)",
                    trigger_type="TIMEOUT_REACHED", state=state,
                    details={"elapsed_seconds": elapsed, "limit": self.timeout_seconds})
            if state.total_cost >= self.budget:
                raise ShackleInterrupt(
                    message=(f"Budget exhausted: ${state.total_cost:.5f} spent "
                             f"(limit: ${self.budget:.2f})"),
                    trigger_type="BUDGET_EXCEEDED", state=state,
                    details={"current_cost": state.total_cost, "limit": self.budget})

    def evaluate_llm_call(self, model: str, input_tokens: int, output_tokens: int, state: ExecutionState) -> None:
        # CRITICAL SECTION: the entire read-decide-mutate-check sequence runs
        # under the per-state RLock. Without this, two concurrent LLM calls
        # on the same Guard would each read pre-mutation total_cost, each
        # write their delta, and each pass the post-mutation `>= self.budget`
        # check (lost update), letting combined spend exceed budget by up to
        # N*(single-call-cost) before anything trips.
        with state._lock:
            pricing = _resolve_pricing(model)
            call_cost = ((input_tokens * pricing["input"]) + (output_tokens * pricing["output"])) / 1_000_000
            # SP/1.0: consult the reference decision function with the
            # PRE-mutation remaining + this call's estimated_cost. decide()
            # returns DENY/budget_overrun if this single call would push
            # remaining negative, which is the case the previous design
            # could not catch under concurrency even after locking (the
            # post-mutation check only fires when we've ALREADY gone over).
            pre_remaining = max(self.budget - state.total_cost, 0.0)
            config = {"budget_usd": self.budget}
            decide_state = {
                "budget_initial_usd": self.budget,
                "budget_remaining_usd": pre_remaining,
                "circuit_tripped": False,
                "seen_nonces": [],
            }
            call = {
                "tool_name": model,
                "params": {},
                "estimated_cost_usd": call_cost,
            }
            # decide() is total under the conformance suite, but the previous
            # implementation silently swallowed any exception with `pass`,
            # which is the exact opposite of "fail closed". Surface the
            # failure on the audit trail and deny the call.
            #
            # decide_checked() also validates the RESULT, so an
            # out-of-contract return (non-pair, unknown verdict, unusable
            # reason) can no longer crash the unpack below, nor slip past the
            # verdict branches into the state mutation that COMMITS the spend.
            state.last_decision = _decide_checked(
                _sp_decide, config, decide_state, call)
            verdict, reason = state.last_decision
            if reason == "decide_unavailable_fail_closed":
                logger.warning(
                    "SHACKLE: decide() raised during cost consultation; "
                    "failing closed (denying call).")
            elif reason.startswith("malformed_decision:"):
                logger.warning(
                    "SHACKLE: decide() returned an out-of-contract result "
                    "during cost consultation (%s); failing closed.", reason)
            # ALLOW-LIST, not a deny-list. Only the exact verdict "ALLOW"
            # releases the call. Previously this read `if verdict == "DENY"`
            # with no else, so ANY other value -- including a verdict this
            # runtime does not recognize -- skipped every branch and fell
            # through to the mutation below.
            if verdict != "ALLOW":
                if reason == "budget_overrun":
                    # This single call would exceed the remaining budget.
                    # Do NOT mutate state -- the call never happened.
                    raise ShackleInterrupt(
                        message=(f"Budget overrun: this call costs ${call_cost:.5f} "
                                 f"but only ${pre_remaining:.5f} remains "
                                 f"(limit: ${self.budget:.2f})"),
                        trigger_type="BUDGET_OVERRUN", state=state,
                        details={"model": model, "call_cost": call_cost,
                                 "remaining": pre_remaining, "limit": self.budget})
                if reason == "budget_exhausted":
                    # No budget left at all; refuse the call without mutating.
                    raise ShackleInterrupt(
                        message=(f"Budget exhausted: ${state.total_cost:.5f} spent "
                                 f"(limit: ${self.budget:.2f})"),
                        trigger_type="BUDGET_EXCEEDED", state=state,
                        details={"model": model, "current_cost": state.total_cost,
                                 "limit": self.budget})
                # Any other DENY from decide() is unexpected on the cost
                # path; fail closed.
                raise ShackleInterrupt(
                    message=f"SHACKLE denied call: {reason}",
                    trigger_type="DECIDE_DENY", state=state,
                    details={"model": model, "reason": reason,
                             "call_cost": call_cost, "limit": self.budget})
            # Allowed by decide(): mutate state, then re-check the
            # post-mutation total against the hard limit. This catches the
            # "this call exactly exhausted the budget" edge case (remaining
            # went from >0 to 0, which is not an overrun, but the call did
            # spend the last dollar and should not be silently allowed).
            state.total_cost += call_cost
            state.input_tokens += input_tokens
            state.output_tokens += output_tokens
            if state.total_cost >= self.budget:
                raise ShackleInterrupt(
                    message=f"Budget breached: ${state.total_cost:.5f} spent (limit: ${self.budget:.2f})",
                    trigger_type="BUDGET_EXCEEDED", state=state,
                    details={"model": model, "current_cost": state.total_cost, "limit": self.budget,
                              "input_tokens": state.input_tokens, "output_tokens": state.output_tokens})

    # decide() reason -> ShackleInterrupt.trigger_type. Any reason absent from
    # this table still DENIES (catch-all DECIDE_DENY below); the table only
    # controls how the denial is LABELLED, never whether it is enforced.
    _DENY_TRIGGERS = {
        "max_repeat_exceeded": "REPETITIVE_TOOL_CALL",
        "budget_exhausted": "BUDGET_EXCEEDED",
        "budget_overrun": "BUDGET_OVERRUN",
        "circuit_open": "CIRCUIT_OPEN",
        "policy_violation:malformed_input": "POLICY_VIOLATION",
        "policy_violation:duplicate_nonce": "DUPLICATE_NONCE",
        "policy_violation:duplicate_resume_no_effect": "POLICY_VIOLATION",
        # The decision RESULT itself was out of contract. Labelled
        # distinctly from a policy denial so an operator can tell "the guard
        # refused this call" from "the guard could not trust its own decision
        # source" on the audit trail.
        "decide_unavailable_fail_closed": "MALFORMED_DECISION",
        "malformed_decision:missing": "MALFORMED_DECISION",
        "malformed_decision:not_a_pair": "MALFORMED_DECISION",
        "malformed_decision:bad_arity": "MALFORMED_DECISION",
        "malformed_decision:verdict_not_a_string": "MALFORMED_DECISION",
        "malformed_decision:unknown_verdict": "MALFORMED_DECISION",
        "malformed_decision:reason_not_a_string": "MALFORMED_DECISION",
        "malformed_decision:unspecified_reason": "MALFORMED_DECISION",
    }

    def evaluate_tool_call(
        self,
        agent_name: str,
        tool_name: str,
        tool_input: Any,
        state: ExecutionState,
        nonce: Any = None,
        estimated_cost_usd: float = 0.0,
    ) -> None:
        # CRITICAL SECTION: total_tool_calls, tool_history[key] = get + 1,
        # and last_decision are all racy under concurrent tool invocations
        # (e.g. a thread-pool executor inside the agent framework dispatching
        # multiple tools in parallel). Same lock as the cost path.
        with state._lock:
            elapsed = time.time() - state.start_time
            state.total_tool_calls += 1

            if elapsed > self.timeout_seconds:
                raise ShackleInterrupt(
                    message=f"Execution timeout: {elapsed:.1f}s elapsed (limit: {self.timeout_seconds}s)",
                    trigger_type="TIMEOUT_REACHED", state=state,
                    details={"elapsed_seconds": elapsed, "limit": self.timeout_seconds})

            input_key = _canonicalize_tool_input(tool_input)  # FIX #1
            key = (tool_name, input_key)
            state.tool_history[key] = state.tool_history.get(key, 0) + 1
            count = state.tool_history[key]

            input_lower = input_key.lower()
            is_error_loop = any(token in input_lower for token in
                                 ("error", "failed", "unauthorized", "401", "403", "500", "timeout"))
            effective_count = count + (1 if is_error_loop and count >= 2 else 0)

            # ---- SP/1.0: consult the reference decision function AND ENFORCE IT ----
            # Map live runtime state onto decide()'s (config, state, call) contract
            # and act on its verdict. Before SP/1.0.1 the verdict was computed,
            # stored to state.last_decision, and then ignored: the raises below
            # re-implemented the repeat rule independently, so a DENY that only
            # decide() could see (circuit_open, duplicate_nonce, malformed_input)
            # was recorded on the audit trail while the call executed anyway.
            params = tool_input if isinstance(tool_input, dict) else {"tool_input": input_key}
            sp_verdict, sp_reason = self._consult_decide(
                tool_name, effective_count, state,
                params=params, nonce=nonce, estimated_cost_usd=estimated_cost_usd,
            )
            state.last_decision = (sp_verdict, sp_reason)

            details = {"agent": agent_name, "tool": tool_name, "input": input_key[:200],
                       "call_count": count, "error_loop": is_error_loop,
                       "decide_reason": sp_reason}

            if sp_verdict == "DENY":
                trigger = self._DENY_TRIGGERS.get(sp_reason, "DECIDE_DENY")
                if trigger == "REPETITIVE_TOOL_CALL":
                    message = (f"Loop of Death detected: '{tool_name}' called "
                               f"{count}x with identical input")
                else:
                    message = f"SHACKLE denied '{tool_name}': {sp_reason}"
                raise ShackleInterrupt(message=message, trigger_type=trigger,
                                       state=state, details=details)

            if sp_verdict == "HITL":
                # Fail closed. decide() returns HITL for opaque/unevaluable
                # context and for the configured HITL modes; the runtime must
                # surface that to a human, never treat it as an ALLOW.
                raise ShackleInterrupt(
                    message=f"SHACKLE requires human review of '{tool_name}': {sp_reason}",
                    trigger_type="HITL_REQUIRED", state=state, details=details)

            # TERMINAL ALLOW-LIST. Release requires the exact
            # verdict "ALLOW" and nothing else. The two branches above are a
            # deny-LIST: before this guard existed, a verdict outside
            # {DENY, HITL, ALLOW} matched neither, fell through, and the tool
            # EXECUTED. normalize_decision() already maps every out-of-contract
            # result to DENY, so reaching here with a non-ALLOW verdict means a
            # future code path bypassed that normalization -- which is exactly
            # the case that must not be allowed to fail open. Belt and braces,
            # deliberately: the invariant is enforced at the point of release,
            # independently of who produced the verdict.
            if sp_verdict != "ALLOW":
                raise ShackleInterrupt(
                    message=(f"SHACKLE denied '{tool_name}': unenforceable verdict "
                             f"{sp_verdict!r} ({sp_reason})"),
                    trigger_type="MALFORMED_DECISION", state=state, details=details)

            state.record_nonce(nonce)

            if state.total_tool_calls >= self.max_tool_calls:
                raise ShackleInterrupt(
                    message=f"Max tool calls reached: {state.total_tool_calls} (limit: {self.max_tool_calls})",
                    trigger_type="MAX_TOOL_CALLS", state=state,
                    details={"total_calls": state.total_tool_calls, "limit": self.max_tool_calls})


def render_hitl_terminal(interrupt: ShackleInterrupt) -> str:
    """Blocking, synchronous HITL prompt. Safe to call from sync code paths.
    NEVER call this directly from an async patched function -- it will
    freeze the event loop. Use _render_hitl_terminal_async there instead.
    """
    TRIGGER_EMOJI = {"REPETITIVE_TOOL_CALL": "R", "BUDGET_EXCEEDED": "B",
                      "TIMEOUT_REACHED": "T", "MAX_TOOL_CALLS": "M"}
    emoji = TRIGGER_EMOJI.get(interrupt.trigger_type, "!")
    console.print()
    console.print(f"SHACKLE CIRCUIT BREAKER: {interrupt.trigger_type} {emoji}")
    console.print("Options: [R] Resume  [S] Skip  [A] Abort")
    valid = {"R", "S", "A"}
    while True:
        try:
            choice = input("Select action (R/S/A): ").strip().upper()
        except (EOFError, OSError):
            # No stdin (CI, daemon, notebook kernel, closed pipe). A breaker
            # that cannot reach a human must ABORT, not crash with an
            # unrelated exception that a framework may swallow.
            console.print("SHACKLE: no interactive operator available; aborting (fail-closed).")
            return "A"
        if choice in valid:
            return choice
        console.print("Invalid choice. Enter R, S, or A.")


# ──────────────────────────────────────────────
# FIX #2 (part A): async-safe HITL prompt
# ──────────────────────────────────────────────
async def _render_hitl_terminal_async(interrupt: ShackleInterrupt) -> str:
    """Async-safe HITL prompt. Offloads the blocking input()/print() call to
    a worker thread via asyncio.to_thread so it does NOT block the event
    loop that other coroutines (other agents, other tool calls) may be
    running on. This is what makes it safe to raise ShackleInterrupt from
    inside a patched acompletion()/arun() without freezing the whole process.
    """
    return await asyncio.to_thread(render_hitl_terminal, interrupt)


def _deny_if_latched(state: "ExecutionState") -> None:
    """Fail closed after an Abort: a swallowed interrupt must not re-arm the run."""
    si = state.pending_interrupt
    if si is not None:
        raise si


def _running_on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


_CONTINUE = object()  # sentinel: "Resume" was chosen, proceed to call the real function


def _handle_interrupt_sync(si: "ShackleInterrupt", state: ExecutionState,
                            reset_cost: bool, skip_message: Optional[Any]) -> Any:
    action = render_hitl_terminal(si)
    if action == "A":
        state.latch_interrupt(si)
        raise si
    if action == "R":
        # An explicit human Resume is the ONLY thing that may unlatch the
        # circuit. Without this, a Resume clears the counters but decide()
        # keeps returning DENY:circuit_open on every subsequent call.
        state.reset_circuit()
        if reset_cost:
            state.total_cost = 0.0
            state.input_tokens = 0
            state.output_tokens = 0
        else:
            state.tool_history.clear()
        return _CONTINUE
    if action == "S":
        return skip_message
    return _CONTINUE


async def _handle_interrupt_async(si: "ShackleInterrupt", state: ExecutionState,
                                   reset_cost: bool, skip_message: Optional[Any]) -> Any:
    action = await _render_hitl_terminal_async(si)
    if action == "A":
        state.latch_interrupt(si)
        raise si
    if action == "R":
        # An explicit human Resume is the ONLY thing that may unlatch the
        # circuit. Without this, a Resume clears the counters but decide()
        # keeps returning DENY:circuit_open on every subsequent call.
        state.reset_circuit()
        if reset_cost:
            state.total_cost = 0.0
            state.input_tokens = 0
            state.output_tokens = 0
        else:
            state.tool_history.clear()
        return _CONTINUE
    if action == "S":
        return skip_message
    return _CONTINUE


def _extract_llm_usage(response: Any) -> Tuple[int, int]:
    usage = response.get("usage", {}) if isinstance(response, dict) else {}
    if hasattr(response, "usage"):
        usage = response.usage
        input_tok = getattr(usage, "prompt_tokens", 0) or 0
        output_tok = getattr(usage, "completion_tokens", 0) or 0
    else:
        input_tok = usage.get("prompt_tokens", 0)
        output_tok = usage.get("completion_tokens", 0)
    return input_tok, output_tok


# ──────────────────────────────────────────────
# FIX #2 (part B) + FIX #3: async patching, fully reentrant (no module globals)
# ──────────────────────────────────────────────
# Each _patch_* function captures whatever function is CURRENTLY installed
# (which, if another Guard scope is already active, is THAT Guard's patched
# wrapper -- not the true original). This makes nested/overlapping Guard
# usage compose correctly instead of clobbering: removal restores exactly
# what was there immediately before this call patched it, in LIFO order via
# Guard's own try/finally. There is no shared mutable module state at all,
# so two Guard scopes (nested, or concurrent across threads/tasks) no longer
# stomp on each other's "original" references.

_LITELLM_SCOPES: Dict[int, Tuple["TriggerEngine", "ExecutionState"]] = {}
_LITELLM_SCOPES_LOCK = threading.Lock()
_LITELLM_ACCOUNTANT: Any = None

def _litellm_scope_for(call_id: Any) -> Optional[Tuple["TriggerEngine", "ExecutionState"]]:
    """Find (and claim) the live Guard scope whose gate admitted this call."""
    if not call_id:
        return None
    with _LITELLM_SCOPES_LOCK:
        scopes = list(_LITELLM_SCOPES.values())
    for engine, state in scopes:
        with state._lock:
            if call_id in state.bypass_call_ids:
                state.bypass_call_ids.discard(call_id)
                return engine, state
    return None

def _install_litellm_accountant(litellm: Any) -> None:
    """Register ONE process-wide success accountant, once. litellm copies
    CustomLogger instances from ``litellm.callbacks`` into its own success
    lists, keyed by class, and never removes them: a per-Guard instance is
    registered on the first Guard and silently shadows every later one (found
    by test: second Guard in a process never accounted a single call). The
    shared instance is inert when no Guard scope is live."""
    global _LITELLM_ACCOUNTANT
    from litellm.integrations.custom_logger import CustomLogger

    if _LITELLM_ACCOUNTANT is None:
        class ShackleAccountant(CustomLogger):
            def _account(self, kw: Dict[str, Any], response_obj: Any) -> None:
                scope = _litellm_scope_for((kw or {}).get("litellm_call_id"))
                if scope is None:
                    return
                engine, state = scope
                in_tok, out_tok = _extract_llm_usage(response_obj)
                try:
                    engine.evaluate_llm_call((kw or {}).get("model") or "default", in_tok, out_tok, state)
                except ShackleInterrupt as si:
                    # Runs on litellm's logging thread: cannot raise into the
                    # caller and must not block on input(). Latch: the very
                    # next gate denies and Guard re-raises at the boundary.
                    state.latch_interrupt(si)

            def log_success_event(self, kwargs, response_obj, start_time, end_time):  # noqa: N803
                self._account(kwargs, response_obj)

            async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):  # noqa: N803
                self._account(kwargs, response_obj)

            def log_failure_event(self, kwargs, response_obj, start_time, end_time):  # noqa: N803
                _litellm_scope_for((kwargs or {}).get("litellm_call_id"))

            async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):  # noqa: N803
                _litellm_scope_for((kwargs or {}).get("litellm_call_id"))

        _LITELLM_ACCOUNTANT = ShackleAccountant()
    installed = (list(getattr(litellm, "callbacks", None) or [])
                 + list(getattr(litellm, "success_callback", None) or []))
    if not any(cb is _LITELLM_ACCOUNTANT for cb in installed):
        if not isinstance(getattr(litellm, "callbacks", None), list):
            litellm.callbacks = []
        litellm.callbacks.append(_LITELLM_ACCOUNTANT)

_IN_PATCHED_LITELLM: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "shackle_in_patched_litellm", default=False)

def _patch_litellm(engine: TriggerEngine, state: ExecutionState) -> Optional[Dict[str, Any]]:
    """Two independent layers, so neither import style escapes the breaker.

    Layer 1 (rebind): replace litellm.completion / acompletion. Sees the
    response, accounts cost synchronously, offers the HITL console. Only
    catches callers that resolve the name at call time.

    Layer 2 (gate + callback): callers that did ``from litellm import
    completion`` BEFORE the Guard armed hold the original function object and
    never touch the rebind. litellm's own @client wrapper, however, resolves
    ``load_credentials_from_list`` from its module globals on every call, sync
    and async, so wrapping that is a call-time choke point no import style can
    skip. The gate enforces (latch / timeout / exhausted budget) before the
    request goes out; a CustomLogger success callback accounts the cost for the
    calls the gate admitted. Calls that came through Layer 1 are marked with a
    ContextVar and are never double counted.

    Layer 2 accounting is eventually consistent: cost lands when litellm's
    success handler runs (milliseconds, off-thread), so a burst of sub-ms calls
    can pass the gate before the previous call is booked. Real network calls
    are orders of magnitude slower than that window.
    """
    try:
        import litellm
    except ImportError:
        logger.debug("litellm not available - skipping LLM hook")
        return None

    prev_completion = getattr(litellm, "completion", None)
    prev_acompletion = getattr(litellm, "acompletion", None)

    def _model_of(args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> str:
        return kwargs.get("model") or (args[0] if args and isinstance(args[0], str) else "default")

    def patched_completion(*args: Any, **kwargs: Any) -> Any:
        _deny_if_latched(state)
        token = _IN_PATCHED_LITELLM.set(True)
        try:
            response = prev_completion(*args, **kwargs)
        finally:
            _IN_PATCHED_LITELLM.reset(token)
        try:
            input_tok, output_tok = _extract_llm_usage(response)
            engine.evaluate_llm_call(_model_of(args, kwargs), input_tok, output_tok, state)
        except ShackleInterrupt as si:
            _handle_interrupt_sync(si, state, reset_cost=True, skip_message=None)
        return response

    async def patched_acompletion(*args: Any, **kwargs: Any) -> Any:
        _deny_if_latched(state)
        token = _IN_PATCHED_LITELLM.set(True)
        try:
            response = await prev_acompletion(*args, **kwargs)
        finally:
            _IN_PATCHED_LITELLM.reset(token)
        try:
            input_tok, output_tok = _extract_llm_usage(response)
            engine.evaluate_llm_call(_model_of(args, kwargs), input_tok, output_tok, state)
        except ShackleInterrupt as si:
            await _handle_interrupt_async(si, state, reset_cost=True, skip_message=None)
        return response

    if prev_completion is not None:
        litellm.completion = patched_completion
    if prev_acompletion is not None:
        litellm.acompletion = patched_acompletion
    logger.info("SHACKLE: Hooked litellm.completion + litellm.acompletion")

    # ---- Layer 2: call-time gate + success-callback accounting ----
    gate_mod, prev_gate, scope_key = None, None, None
    try:
        import litellm.utils as _lu
        prev_gate = getattr(_lu, "load_credentials_from_list", None)
        if prev_gate is not None:
            def gate(call_kwargs: Any, *g_args: Any, **g_kwargs: Any) -> Any:
                if not _IN_PATCHED_LITELLM.get():
                    _deny_if_latched(state)
                    try:
                        engine.precheck_llm_call(state)
                    except ShackleInterrupt as si:
                        if _running_on_event_loop():
                            # input() here would freeze the loop; fail closed.
                            state.latch_interrupt(si)
                            raise
                        _handle_interrupt_sync(si, state, reset_cost=True, skip_message=None)
                    call_id = call_kwargs.get("litellm_call_id") if isinstance(call_kwargs, dict) else None
                    if call_id:
                        with state._lock:
                            state.bypass_call_ids.add(call_id)
                return prev_gate(call_kwargs, *g_args, **g_kwargs)

            _install_litellm_accountant(litellm)
            scope_key = id(state)
            with _LITELLM_SCOPES_LOCK:
                _LITELLM_SCOPES[scope_key] = (engine, state)
            _lu.load_credentials_from_list = gate
            gate_mod = _lu
            logger.info("SHACKLE: Installed litellm call-time gate + success accounting")
    except Exception as exc:  # feature-detect: litellm internals move between versions
        logger.warning("SHACKLE: litellm call-time gate unavailable (%s); early-imported "
                       "`from litellm import completion` references are NOT covered.", exc)
        gate_mod, prev_gate, scope_key = None, None, None

    return {"module": litellm, "completion": prev_completion, "acompletion": prev_acompletion,
            "gate_mod": gate_mod, "gate": prev_gate, "scope_key": scope_key}

def _unpatch_litellm(saved: Optional[Dict[str, Any]]) -> None:
    if not saved:
        return
    mod = saved["module"]
    if saved.get("completion") is not None:
        mod.completion = saved["completion"]
    if saved.get("acompletion") is not None:
        mod.acompletion = saved["acompletion"]
    if saved.get("gate_mod") is not None and saved.get("gate") is not None:
        saved["gate_mod"].load_credentials_from_list = saved["gate"]
    if saved.get("scope_key") is not None:
        with _LITELLM_SCOPES_LOCK:
            _LITELLM_SCOPES.pop(saved["scope_key"], None)


def _patch_basetool(engine: TriggerEngine, state: ExecutionState) -> Optional[Dict[str, Any]]:
    try:
        from langchain_core.tools import BaseTool
    except ImportError:
        logger.debug("langchain_core not available - skipping tool hook")
        return None

    prev_run = getattr(BaseTool, "run", None)
    prev_arun = getattr(BaseTool, "arun", None)

    def patched_run(self_: Any, *args: Any, **kwargs: Any) -> Any:
        _deny_if_latched(state)
        tool_name = getattr(self_, "name", "unknown_tool")
        tool_input = args[0] if args else kwargs
        try:
            engine.evaluate_tool_call("Agent", tool_name, tool_input, state)
        except ShackleInterrupt as si:
            result = _handle_interrupt_sync(
                si, state, reset_cost=False,
                skip_message="[SHACKLE] Tool execution skipped by operator. Proceed to next step.")
            if result is not _CONTINUE:
                return result
        return prev_run(self_, *args, **kwargs)

    async def patched_arun(self_: Any, *args: Any, **kwargs: Any) -> Any:
        _deny_if_latched(state)
        tool_name = getattr(self_, "name", "unknown_tool")
        tool_input = args[0] if args else kwargs
        try:
            engine.evaluate_tool_call("Agent", tool_name, tool_input, state)
        except ShackleInterrupt as si:
            result = await _handle_interrupt_async(
                si, state, reset_cost=False,
                skip_message="[SHACKLE] Tool execution skipped by operator. Proceed to next step.")
            if result is not _CONTINUE:
                return result
        return await prev_arun(self_, *args, **kwargs)

    if prev_run is not None:
        BaseTool.run = patched_run
    if prev_arun is not None:
        BaseTool.arun = patched_arun
    logger.info("SHACKLE: Hooked BaseTool.run + BaseTool.arun")
    return {"cls": BaseTool, "run": prev_run, "arun": prev_arun}


def _unpatch_basetool(saved: Optional[Dict[str, Any]]) -> None:
    if not saved:
        return
    cls = saved["cls"]
    if saved.get("run") is not None:
        cls.run = saved["run"]
    if saved.get("arun") is not None:
        cls.arun = saved["arun"]


def _patch_crewai_agent(engine: TriggerEngine, state: ExecutionState) -> Optional[Dict[str, Any]]:
    """Experimental: Hook CrewAI Agent.execute_task to catch internal
    reasoning loops that never surface a tool call (Manager Agent loops).
    CrewAI's execute_task is sync-only as of this writing; no async variant
    to patch.
    """
    try:
        from crewai.agent import Agent
    except ImportError:
        logger.debug("crewai not available - skipping Agent hook")
        return None

    prev_execute_task = getattr(Agent, "execute_task", None)

    def patched_execute_task(self_: Any, *args: Any, **kwargs: Any) -> Any:
        _deny_if_latched(state)
        agent_name = getattr(self_, "role", "UnknownAgent")
        task_desc = str(args[0])[:200] if args else "planning"
        try:
            engine.evaluate_tool_call(agent_name, "internal_reasoning", task_desc, state)
        except ShackleInterrupt as si:
            result = _handle_interrupt_sync(
                si, state, reset_cost=False,
                skip_message={"status": "skipped", "output": "Task bypassed by SHACKLE circuit breaker."})
            if result is not _CONTINUE:
                return result
        return prev_execute_task(self_, *args, **kwargs)

    if prev_execute_task is not None:
        Agent.execute_task = patched_execute_task
    logger.info("SHACKLE: Hooked CrewAI Agent.execute_task (Manager loop protection - experimental)")
    return {"cls": Agent, "execute_task": prev_execute_task}


def _unpatch_crewai_agent(saved: Optional[Dict[str, Any]]) -> None:
    if not saved:
        return
    if saved.get("execute_task") is not None:
        saved["cls"].execute_task = saved["execute_task"]


_SKIP_TOOL_MSG = "[SHACKLE] Tool execution skipped by operator. Proceed to next step."

def _hook_interrupt(si: "ShackleInterrupt", state: ExecutionState,
                    reset_cost: bool, skip_message: Optional[Any]) -> Any:
    """HITL for framework hook contexts, which are frequently invoked from an
    async seam. Blocking on input() there freezes the event loop, so on a loop
    thread the breaker fails closed (latch + raise) instead of prompting."""
    if _running_on_event_loop():
        state.latch_interrupt(si)
        raise si
    return _handle_interrupt_sync(si, state, reset_cost=reset_cost, skip_message=skip_message)

def _patch_crewai_hooks(engine: TriggerEngine, state: ExecutionState) -> Optional[Dict[str, Any]]:
    """Enforce through CrewAI's own interception API.

    Modern CrewAI (1.x) routes ``openai/...`` models through native provider
    classes (litellm is never touched) and its tools subclass
    ``crewai.tools.BaseTool``, NOT ``langchain_core.tools.BaseTool``. Both of
    SHACKLE's original hooks were therefore blind to it. CrewAI exposes global
    before-tool / before-LLM / after-LLM hooks; this uses them.

    Three properties of that API shape the design (each found by live test):

    1. CrewAI swallows every exception a hook raises EXCEPT ``HookAborted``
       (fail-open by design, to shield the framework from buggy hooks). A
       ShackleInterrupt -- or any bug in this code -- raised from a hook would
       be a silent no-op. Denials are expressed as ``HookAborted`` (legacy
       ``return False`` on builds without it) and every hook body is wrapped so
       an internal error DENIES instead of leaking out to be swallowed.
    2. ``after_llm_call`` fires only for the final text response, NOT for the
       intermediate tool-calling turns. Cost is therefore settled from the
       LLM's cumulative token counters at every boundary that does fire
       (before_llm for the previous turn, before_tool for the turn that asked
       for the tool, after_llm for the last one), so budget bites per turn.
    3. Hooks run on worker threads under ``akickoff``; on a live event-loop
       thread the breaker never prompts (fail closed) since input() would
       freeze the loop.
    """
    try:
        from crewai.hooks import (
            register_before_tool_call_hook, unregister_before_tool_call_hook,
            register_before_llm_call_hook, unregister_before_llm_call_hook,
            register_after_llm_call_hook, unregister_after_llm_call_hook,
        )
    except ImportError:
        logger.debug("crewai.hooks not available - skipping CrewAI native hooks")
        return None
    try:
        from crewai.hooks import HookAborted
    except ImportError:
        HookAborted = None  # type: ignore[assignment]

    def _deny(si: "ShackleInterrupt") -> Any:
        if HookAborted is not None:
            raise HookAborted(f"SHACKLE {si.trigger_type}: {si}", source="shackle")
        return False

    def _fail_closed(fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
        def wrapper(ctx: Any) -> Any:
            try:
                return fn(ctx)
            except ShackleInterrupt as si:
                state.latch_interrupt(si)
                return _deny(si)
            except Exception as exc:
                if HookAborted is not None and isinstance(exc, HookAborted):
                    raise
                logger.exception("SHACKLE: internal error in CrewAI hook; failing closed")
                si = ShackleInterrupt(
                    message=f"internal error in CrewAI hook: {exc!r}",
                    trigger_type="HOOK_ERROR", state=state, details={"error": repr(exc)})
                state.latch_interrupt(si)
                return _deny(si)
        wrapper.__name__ = getattr(fn, "__name__", "shackle_hook")
        return wrapper

    last_usage: Dict[int, Tuple[int, int]] = {}
    warned_no_usage = set()

    def _usage(llm: Any) -> Optional[Tuple[int, int]]:
        try:
            u = llm.get_token_usage_summary()
            return int(u.prompt_tokens), int(u.completion_tokens)
        except Exception:
            return None

    def _baseline(llm: Any) -> None:
        if id(llm) not in last_usage:
            snap = _usage(llm)
            if snap is not None:
                last_usage[id(llm)] = snap

    def _settle(llm: Any, ctx: Any = None) -> None:
        """Book tokens spent since the last boundary. Raises ShackleInterrupt."""
        if llm is None:
            return
        snap = _usage(llm)
        if snap is not None:
            prev = last_usage.get(id(llm), snap)
            last_usage[id(llm)] = snap
            d_in, d_out = max(snap[0] - prev[0], 0), max(snap[1] - prev[1], 0)
        elif ctx is not None and getattr(ctx, "response", None) is not None:
            # Provider exposes no counters. Never skip accounting silently:
            # estimate (~4 chars/token) so the budget still bites.
            d_in = len(json.dumps(getattr(ctx, "messages", None) or [], default=str)) // 4
            d_out = len(str(ctx.response)) // 4
            if id(llm) not in warned_no_usage:
                warned_no_usage.add(id(llm))
                logger.warning("SHACKLE: LLM %r exposes no token usage; estimating from text length.",
                               getattr(llm, "model", llm))
            last_usage[id(llm)] = (0, 0)
        else:
            if id(llm) not in warned_no_usage:
                warned_no_usage.add(id(llm))
                logger.warning("SHACKLE: LLM %r exposes no token usage; per-turn budget "
                               "accounting is unavailable for it.", getattr(llm, "model", llm))
            return
        if d_in or d_out:
            engine.evaluate_llm_call(str(getattr(llm, "model", None) or "default"), d_in, d_out, state)

    def _enforce(check: Callable[[], None], reset_cost: bool, skip_message: Optional[Any]) -> Any:
        """Run ``check`` (raises ShackleInterrupt to deny) under HITL. Returns
        None to allow, False to block just this operation (operator Skip), and
        denies (HookAborted) on Abort or when a prior Abort is latched."""
        si = state.pending_interrupt
        if si is None:
            try:
                check()
                return None
            except ShackleInterrupt as raised:
                try:
                    res = _hook_interrupt(raised, state, reset_cost, skip_message)
                except ShackleInterrupt as aborted:
                    si = aborted
                else:
                    if res is _CONTINUE or skip_message is None:
                        return None
                    return False
        return _deny(si)

    @_fail_closed
    def before_tool(ctx: Any) -> Any:
        agent = getattr(ctx, "agent", None)
        tool_input = ctx.tool_input if isinstance(ctx.tool_input, dict) else {}

        def check() -> None:
            _settle(getattr(agent, "llm", None))   # the turn that requested this tool
            engine.evaluate_tool_call(getattr(agent, "role", None) or "Agent",
                                      ctx.tool_name, tool_input, state)
        return _enforce(check, False, _SKIP_TOOL_MSG)

    @_fail_closed
    def before_llm(ctx: Any) -> Any:
        llm = getattr(ctx, "llm", None)
        _baseline(llm)

        def check() -> None:
            _settle(llm)                            # the previous turn
            engine.precheck_llm_call(state)
        return _enforce(check, True, None)

    @_fail_closed
    def after_llm(ctx: Any) -> Any:
        return _enforce(lambda: _settle(getattr(ctx, "llm", None), ctx), True, None)

    register_before_tool_call_hook(before_tool)
    register_before_llm_call_hook(before_llm)
    register_after_llm_call_hook(after_llm)
    logger.info("SHACKLE: Hooked CrewAI native before_tool / before_llm / after_llm hooks")
    return {"before_tool": (unregister_before_tool_call_hook, before_tool),
            "before_llm": (unregister_before_llm_call_hook, before_llm),
            "after_llm": (unregister_after_llm_call_hook, after_llm)}

def _unpatch_crewai_hooks(saved: Optional[Dict[str, Any]]) -> None:
    if not saved:
        return
    for unregister, hook in saved.values():
        try:
            unregister(hook)
        except Exception:  # pragma: no cover
            pass


def _apply_patches(engine: TriggerEngine, state: ExecutionState) -> Dict[str, Any]:
    """Apply all available runtime patches. Returns a per-call token that
    must be passed to _remove_patches to reverse exactly this application
    (and only this one -- safe under nesting/concurrency)."""
    return {
        "litellm": _patch_litellm(engine, state),
        "basetool": _patch_basetool(engine, state),
        "crewai": _patch_crewai_agent(engine, state),
        "crewai_hooks": _patch_crewai_hooks(engine, state),
    }


def _remove_patches(saved: Dict[str, Any]) -> None:
    _unpatch_litellm(saved.get("litellm"))
    _unpatch_basetool(saved.get("basetool"))
    _unpatch_crewai_agent(saved.get("crewai"))
    _unpatch_crewai_hooks(saved.get("crewai_hooks"))


class ShackleCoverageError(RuntimeError):
    """Raised by Guard(strict=True) when a loaded framework has no live hook."""

# framework module -> the patch key that must be live for it to be governed
_HOOKED_FRAMEWORKS = {
    "crewai": ("crewai_hooks", "CrewAI tools and LLM calls"),
    "litellm": ("litellm", "litellm completion/acompletion"),
    "langchain_core": ("basetool", "LangChain/LangGraph BaseTool.run/arun"),
}
# frameworks with no native hook in Guard: only covered if calls route via litellm
_UNHOOKED_FRAMEWORKS = ("autogen", "autogen_core", "autogen_agentchat", "smolagents")

def _coverage_report(saved: Dict[str, Any]) -> Dict[str, Any]:
    """What is actually governed right now. An armed banner that intercepts
    nothing is a fail-open, so gaps are reported instead of assumed away."""
    hooked, gaps = [], []
    for fw, (key, what) in _HOOKED_FRAMEWORKS.items():
        if fw not in sys.modules:
            continue
        if saved.get(key):
            hooked.append(fw)
        else:
            gaps.append((fw, f"{fw} is loaded but its hook ({key}) could not attach: {what} are NOT governed"))
    for fw in _UNHOOKED_FRAMEWORKS:
        if fw in sys.modules:
            gaps.append((fw, f"{fw} is loaded; Guard has no native hook for it. Governed only if its "
                             f"LLM calls route through litellm (see shackle.autogen_shackle_wrapper)"))
    if not any(v for v in saved.values()):
        gaps.append(("none", "no framework hooks attached: this Guard is inert"))
    return {"hooked": hooked, "gaps": gaps}


class Guard:
    """
    One-line circuit breaker for autonomous agent workflows.

    Usage::

        from shackle import Guard

        @Guard(budget=0.25, max_repeat_calls=3, timeout_seconds=180)
        def run_agents():
            my_crew.kickoff()

        run_agents()

    Reentrant: nested or concurrent Guard scopes compose (each wraps
    whatever is currently installed) instead of clobbering each other.
    Covers both sync and async call paths (litellm.completion/acompletion,
    BaseTool.run/arun). A Guard-decorated coroutine function is awaited and
    unpatched only after the coroutine completes.
    """
    def __init__(self, budget: float = 0.20, max_repeat_calls: int = 3,
                 timeout_seconds: float = 180.0, max_tool_calls: int = 50,
                 strict: bool = False):
        self.strict = strict
        self.last_coverage: Dict[str, Any] = {"hooked": [], "gaps": []}
        self.engine = TriggerEngine(budget=budget, max_repeat_calls=max_repeat_calls,
                                     timeout_seconds=timeout_seconds, max_tool_calls=max_tool_calls)

    def _arm_banner(self) -> None:
        console.print(
            f"SHACKLE armed - budget: ${self.engine.budget:.2f} | "
            f"repeat limit: {self.engine.max_repeat_calls}x | "
            f"timeout: {self.engine.timeout_seconds}s")

    def _check_coverage(self, saved: Dict[str, Any]) -> None:
        report = self.last_coverage = _coverage_report(saved)
        for fw, msg in report["gaps"]:
            logger.warning("SHACKLE coverage gap: %s", msg)
            console.print(f"SHACKLE WARNING - coverage gap: {msg}")
        if report["gaps"] and self.strict:
            raise ShackleCoverageError(
                "; ".join(msg for _, msg in report["gaps"]))

    def _complete_banner(self, state: ExecutionState) -> None:
        console.print(
            f"SHACKLE SESSION COMPLETE - spent ${state.total_cost:.5f} | "
            f"tokens in: {state.input_tokens:,} out: {state.output_tokens:,} | "
            f"tool calls: {state.total_tool_calls}")

    def __call__(self, func: Callable[..., Any]) -> Callable[..., Any]:
        # FIX: async-decoration support. If the decorated entrypoint is itself
        # a coroutine function, return an async wrapper that awaits the work
        # and only removes patches after the coroutine actually completes.
        # (The previous sync-only wrapper returned the coroutine unawaited and
        # ran its finally/unpatch before the async work finished.)
        if asyncio.iscoroutinefunction(func):
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                state = ExecutionState()
                saved = _apply_patches(self.engine, state)
                try:
                    self._check_coverage(saved)
                    self._arm_banner()
                    result = await func(*args, **kwargs)
                except ShackleInterrupt as si:
                    console.print(f"SHACKLE: Execution aborted - {si.trigger_type}")
                    raise
                except Exception as exc:
                    if state.pending_interrupt is not None:
                        console.print(f"SHACKLE: Execution aborted - {state.pending_interrupt.trigger_type}")
                        raise state.pending_interrupt from exc
                    raise
                else:
                    if state.pending_interrupt is not None:
                        # A framework swallowed the Abort. Surface the real trigger.
                        console.print(f"SHACKLE: Execution aborted - {state.pending_interrupt.trigger_type}")
                        raise state.pending_interrupt
                    return result
                finally:
                    _remove_patches(saved)
                    self._complete_banner(state)
            async_wrapper.__name__ = func.__name__
            async_wrapper.__doc__ = func.__doc__
            async_wrapper.__wrapped__ = func
            return async_wrapper

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            state = ExecutionState()
            saved = _apply_patches(self.engine, state)
            try:
                self._check_coverage(saved)
                self._arm_banner()
                result = func(*args, **kwargs)
            except ShackleInterrupt as si:
                console.print(f"SHACKLE: Execution aborted - {si.trigger_type}")
                raise
            except Exception as exc:
                if state.pending_interrupt is not None:
                    console.print(f"SHACKLE: Execution aborted - {state.pending_interrupt.trigger_type}")
                    raise state.pending_interrupt from exc
                raise
            else:
                if state.pending_interrupt is not None:
                    # A framework swallowed the Abort. Surface the real trigger.
                    console.print(f"SHACKLE: Execution aborted - {state.pending_interrupt.trigger_type}")
                    raise state.pending_interrupt
                return result
            finally:
                _remove_patches(saved)
                self._complete_banner(state)
        wrapper.__name__ = func.__name__
        wrapper.__doc__ = func.__doc__
        wrapper.__wrapped__ = func
        return wrapper
