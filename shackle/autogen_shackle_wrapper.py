"""
SHACKLE SP/1.0 - AutoGen wrapper.
Copyright (C) 2026  Dante Bullock, Sovereign Logic.  AGPL-3.0-or-later.

Governs Microsoft AutoGen agents with SHACKLE SP/1.0. Every wrapped tool call is
evaluated by the real shackle.core.TriggerEngine (budget / repeat / timeout /
tool-count), and a ShackleInterrupt is surfaced as ShackleBlocked.

Design notes:
  * AutoGen is an OPTIONAL dependency. This module imports and its tool guarding
    works WITHOUT autogen installed (create_shackle_agent raises a clear error if
    AutoGen is missing, but wrap_tool / guard_tool_call do not need it).
  * Enforcement uses TriggerEngine + a per-wrapper ExecutionState (reentrant, no
    module globals). Canonical tool-input dedup uses _canonicalize_tool_input so
    dict key ordering cannot evade loop detection.
  * Interactive terminal HITL is available for local sync runs via
    render_hitl_terminal; automated contexts fail closed (raise).

Usage:
    from shackle.autogen_shackle_wrapper import wrap_tool, create_shackle_agent

    @wrap_tool(budget=0.50, max_repeat_calls=3)
    def web_search(query: str):
        return real_search(query)

    agent = create_shackle_agent(name="Researcher", llm_config=cfg,
                                 budget=0.50, max_repeat_calls=3)
"""

from __future__ import annotations

import asyncio
import functools
import threading
from typing import Any, Callable, Optional

from shackle.core import (
    TriggerEngine,
    ExecutionState,
    ShackleInterrupt,
    _canonicalize_tool_input,
    render_hitl_terminal,
)

try:
    from autogen import AssistantAgent  # type: ignore
    _AUTOGEN = True
except Exception:  # pragma: no cover - autogen optional
    try:
        from autogen_agentchat.agents import AssistantAgent  # type: ignore
        _AUTOGEN = True
    except Exception:
        AssistantAgent = None  # type: ignore
        _AUTOGEN = False


class ShackleBlocked(Exception):
    """Raised when SHACKLE blocks an AutoGen tool call."""

    def __init__(self, trigger_type: str, message: str) -> None:
        self.trigger_type = trigger_type
        self.message = message
        super().__init__(f"SHACKLE {trigger_type}: {message}")


def guard_tool_call(
    engine: TriggerEngine,
    state: ExecutionState,
    tool_name: str,
    tool_input: Any,
    *,
    agent_name: str = "autogen",
    interactive_hitl: bool = False,
    estimated_cost_usd: float = 0.0,
) -> None:
    """
    Evaluate a single tool call against the SHACKLE engine.

    Raises ShackleBlocked when the circuit breaker trips. When interactive_hitl
    is True and running in a sync context, a terminal prompt is shown; choosing
    to skip/abort still raises so the caller never silently proceeds past a trip.
    """
    try:
        engine.evaluate_tool_call(
            agent_name=agent_name,
            tool_name=tool_name,
            tool_input=tool_input,
            state=state,
            estimated_cost_usd=estimated_cost_usd,
        )
    except ShackleInterrupt as si:
        if interactive_hitl:
            try:
                render_hitl_terminal(si)
            except Exception:
                pass
        raise ShackleBlocked(si.trigger_type, str(si)) from si


def wrap_tool(
    func: Optional[Callable] = None,
    *,
    budget: float = 0.25,
    max_repeat_calls: int = 3,
    timeout_seconds: float = 180.0,
    max_tool_calls: int = 50,
    interactive_hitl: bool = False,
    cost_per_call: float = 0.0,
    cost_fn: Optional[Callable[..., float]] = None,
) -> Callable:
    """
    Decorator that guards any function (sync or async) as a SHACKLE-governed
    AutoGen tool. Each wrapped callable gets its own TriggerEngine + ExecutionState
    so repeat/budget/timeout limits are tracked per tool across invocations.

    ``budget`` is enforced by charging each call ``cost_fn(*args, **kwargs)`` if
    given, else ``cost_per_call``. Both default to 0, in which case a tool spends
    nothing and only the repeat/timeout/call-count limits apply — which is the
    pre-SP/1.0.1 behaviour, except that it is now what the signature says. The
    old code accepted ``budget=`` and then never priced a call, so the budget
    could not be reached and the parameter was decorative.

    Works with or without AutoGen installed.
    """

    def decorate(fn: Callable) -> Callable:
        engine = TriggerEngine(
            budget=budget,
            max_repeat_calls=max_repeat_calls,
            timeout_seconds=timeout_seconds,
            max_tool_calls=max_tool_calls,
        )
        tool_name = getattr(fn, "__name__", "autogen_tool")
        # ExecutionState.start_time defaults to time.time() AT CONSTRUCTION.
        # Building the state here — at decoration time, i.e. at import — meant
        # timeout_seconds was measured from when the module was imported, not
        # from when the tool was first used. A process that imports its tools
        # at startup and runs an agent an hour later tripped TIMEOUT_REACHED on
        # the very first call. The state is created on first invocation instead.
        state_holder: list = []
        state_lock = threading.Lock()

        def _get_state() -> ExecutionState:
            with state_lock:
                if not state_holder:
                    state_holder.append(ExecutionState())
                return state_holder[0]

        def _cost(args, kwargs) -> float:
            if cost_fn is None:
                return cost_per_call
            return float(cost_fn(*args, **kwargs))

        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                state = _get_state()
                tool_input = _canonicalize_tool_input({"args": args, "kwargs": kwargs})
                cost = _cost(args, kwargs)
                guard_tool_call(engine, state, tool_name, tool_input,
                                interactive_hitl=interactive_hitl,
                                estimated_cost_usd=cost)
                result = await fn(*args, **kwargs)
                if cost:
                    with state._lock:
                        state.total_cost += cost
                return result

            wrapper: Callable = async_wrapper
        else:

            @functools.wraps(fn)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                state = _get_state()
                tool_input = _canonicalize_tool_input({"args": args, "kwargs": kwargs})
                cost = _cost(args, kwargs)
                guard_tool_call(engine, state, tool_name, tool_input,
                                interactive_hitl=interactive_hitl,
                                estimated_cost_usd=cost)
                result = fn(*args, **kwargs)
                if cost:
                    with state._lock:
                        state.total_cost += cost
                return result

            wrapper = sync_wrapper

        def _reset() -> None:
            """Drop the accumulated state (counters, spend, clock)."""
            with state_lock:
                state_holder.clear()

        wrapper.shackle_engine = engine          # type: ignore[attr-defined]
        wrapper.shackle_state = _get_state       # type: ignore[attr-defined]
        wrapper.shackle_reset = _reset           # type: ignore[attr-defined]
        return wrapper

    # Support both @wrap_tool and @wrap_tool(...)
    if func is not None and callable(func):
        return decorate(func)
    return decorate


def create_shackle_agent(
    name: str = "ShackledAgent",
    system_message: str = "You are a governed autonomous agent protected by SHACKLE.",
    llm_config: Optional[dict] = None,
    budget: float = 0.25,
    max_repeat_calls: int = 3,
    timeout_seconds: float = 180.0,
    max_tool_calls: int = 50,
    **autogen_kwargs: Any,
):
    """
    Factory for a SHACKLE-governed AutoGen AssistantAgent.

    Returns a real AutoGen AssistantAgent plus an attached .shackle_engine and
    .shackle_state; register tools wrapped with wrap_tool() on it so every tool
    call is governed. Raises RuntimeError if AutoGen is not installed.
    """
    if not _AUTOGEN or AssistantAgent is None:
        raise RuntimeError(
            "AutoGen is not installed. `pip install pyautogen` (or autogen-agentchat) "
            "to use create_shackle_agent; wrap_tool works without AutoGen."
        )
    agent = AssistantAgent(
        name=name,
        system_message=system_message,
        llm_config=llm_config,
        **autogen_kwargs,
    )
    # Attach a shared engine/state for tools that want to consult it.
    agent.shackle_engine = TriggerEngine(
        budget=budget,
        max_repeat_calls=max_repeat_calls,
        timeout_seconds=timeout_seconds,
        max_tool_calls=max_tool_calls,
    )
    agent.shackle_state = ExecutionState()
    return agent
