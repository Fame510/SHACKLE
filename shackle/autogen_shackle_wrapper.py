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
) -> Callable:
    """
    Decorator that guards any function (sync or async) as a SHACKLE-governed
    AutoGen tool. Each wrapped callable gets its own TriggerEngine + ExecutionState
    so repeat/budget/timeout limits are tracked per tool across invocations.

    Works with or without AutoGen installed.
    """

    def decorate(fn: Callable) -> Callable:
        engine = TriggerEngine(
            budget=budget,
            max_repeat_calls=max_repeat_calls,
            timeout_seconds=timeout_seconds,
            max_tool_calls=max_tool_calls,
        )
        state = ExecutionState()
        tool_name = getattr(fn, "__name__", "autogen_tool")

        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                tool_input = _canonicalize_tool_input({"args": args, "kwargs": kwargs})
                guard_tool_call(engine, state, tool_name, tool_input,
                                interactive_hitl=interactive_hitl)
                return await fn(*args, **kwargs)

            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            tool_input = _canonicalize_tool_input({"args": args, "kwargs": kwargs})
            guard_tool_call(engine, state, tool_name, tool_input,
                            interactive_hitl=interactive_hitl)
            return fn(*args, **kwargs)

        return sync_wrapper

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
