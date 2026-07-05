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
import json
import sys
import time
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field

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


@dataclass
class ExecutionState:
    """Live telemetry state tracked entirely in-process."""
    total_cost: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    start_time: float = field(default_factory=time.time)
    total_tool_calls: int = 0
    tool_history: Dict[Tuple[str, str], int] = field(default_factory=dict)


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

    def evaluate_llm_call(self, model: str, input_tokens: int, output_tokens: int, state: ExecutionState) -> None:
        pricing = MODEL_PRICING.get(model.lower(), MODEL_PRICING["default"])
        call_cost = ((input_tokens * pricing["input"]) + (output_tokens * pricing["output"])) / 1_000_000
        state.total_cost += call_cost
        state.input_tokens += input_tokens
        state.output_tokens += output_tokens
        if state.total_cost >= self.budget:
            raise ShackleInterrupt(
                message=f"Budget breached: ${state.total_cost:.5f} spent (limit: ${self.budget:.2f})",
                trigger_type="BUDGET_EXCEEDED", state=state,
                details={"model": model, "current_cost": state.total_cost, "limit": self.budget,
                          "input_tokens": state.input_tokens, "output_tokens": state.output_tokens})

    def evaluate_tool_call(self, agent_name: str, tool_name: str, tool_input: Any, state: ExecutionState) -> None:
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

        if effective_count >= self.max_repeat_calls:
            raise ShackleInterrupt(
                message=f"Loop of Death detected: '{tool_name}' called {count}x with identical input",
                trigger_type="REPETITIVE_TOOL_CALL", state=state,
                details={"agent": agent_name, "tool": tool_name, "input": input_key[:200],
                          "call_count": count, "error_loop": is_error_loop})

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
        choice = input("Select action (R/S/A): ").strip().upper()
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


_CONTINUE = object()  # sentinel: "Resume" was chosen, proceed to call the real function


def _handle_interrupt_sync(si: "ShackleInterrupt", state: ExecutionState,
                            reset_cost: bool, skip_message: Optional[Any]) -> Any:
    action = render_hitl_terminal(si)
    if action == "A":
        raise si
    if action == "R":
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
        raise si
    if action == "R":
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

def _patch_litellm(engine: TriggerEngine, state: ExecutionState) -> Optional[Dict[str, Any]]:
    try:
        import litellm
    except ImportError:
        logger.debug("litellm not available - skipping LLM hook")
        return None

    prev_completion = getattr(litellm, "completion", None)
    prev_acompletion = getattr(litellm, "acompletion", None)

    def patched_completion(*args: Any, **kwargs: Any) -> Any:
        response = prev_completion(*args, **kwargs)
        try:
            model = kwargs.get("model", "default")
            input_tok, output_tok = _extract_llm_usage(response)
            engine.evaluate_llm_call(model, input_tok, output_tok, state)
        except ShackleInterrupt as si:
            _handle_interrupt_sync(si, state, reset_cost=True, skip_message=None)
        return response

    async def patched_acompletion(*args: Any, **kwargs: Any) -> Any:
        response = await prev_acompletion(*args, **kwargs)
        try:
            model = kwargs.get("model", "default")
            input_tok, output_tok = _extract_llm_usage(response)
            engine.evaluate_llm_call(model, input_tok, output_tok, state)
        except ShackleInterrupt as si:
            await _handle_interrupt_async(si, state, reset_cost=True, skip_message=None)
        return response

    if prev_completion is not None:
        litellm.completion = patched_completion
    if prev_acompletion is not None:
        litellm.acompletion = patched_acompletion
    logger.info("SHACKLE: Hooked litellm.completion + litellm.acompletion")
    return {"module": litellm, "completion": prev_completion, "acompletion": prev_acompletion}


def _unpatch_litellm(saved: Optional[Dict[str, Any]]) -> None:
    if not saved:
        return
    mod = saved["module"]
    if saved.get("completion") is not None:
        mod.completion = saved["completion"]
    if saved.get("acompletion") is not None:
        mod.acompletion = saved["acompletion"]


def _patch_basetool(engine: TriggerEngine, state: ExecutionState) -> Optional[Dict[str, Any]]:
    try:
        from langchain_core.tools import BaseTool
    except ImportError:
        logger.debug("langchain_core not available - skipping tool hook")
        return None

    prev_run = getattr(BaseTool, "run", None)
    prev_arun = getattr(BaseTool, "arun", None)

    def patched_run(self_: Any, *args: Any, **kwargs: Any) -> Any:
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


def _apply_patches(engine: TriggerEngine, state: ExecutionState) -> Dict[str, Any]:
    """Apply all available runtime patches. Returns a per-call token that
    must be passed to _remove_patches to reverse exactly this application
    (and only this one -- safe under nesting/concurrency)."""
    return {
        "litellm": _patch_litellm(engine, state),
        "basetool": _patch_basetool(engine, state),
        "crewai": _patch_crewai_agent(engine, state),
    }


def _remove_patches(saved: Dict[str, Any]) -> None:
    _unpatch_litellm(saved.get("litellm"))
    _unpatch_basetool(saved.get("basetool"))
    _unpatch_crewai_agent(saved.get("crewai"))


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
                 timeout_seconds: float = 180.0, max_tool_calls: int = 50):
        self.engine = TriggerEngine(budget=budget, max_repeat_calls=max_repeat_calls,
                                     timeout_seconds=timeout_seconds, max_tool_calls=max_tool_calls)

    def _arm_banner(self) -> None:
        console.print(
            f"SHACKLE armed - budget: ${self.engine.budget:.2f} | "
            f"repeat limit: {self.engine.max_repeat_calls}x | "
            f"timeout: {self.engine.timeout_seconds}s")

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
                    self._arm_banner()
                    return await func(*args, **kwargs)
                except ShackleInterrupt as si:
                    console.print(f"SHACKLE: Execution aborted - {si.trigger_type}")
                    raise
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
                self._arm_banner()
                result = func(*args, **kwargs)
                return result
            except ShackleInterrupt as si:
                console.print(f"SHACKLE: Execution aborted - {si.trigger_type}")
                raise
            finally:
                _remove_patches(saved)
                self._complete_banner(state)
        wrapper.__name__ = func.__name__
        wrapper.__doc__ = func.__doc__
        wrapper.__wrapped__ = func
        return wrapper
