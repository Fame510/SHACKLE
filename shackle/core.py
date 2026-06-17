"""
SHACKLE — Runtime Circuit Breaker for Autonomous AI Agents.
Copyright (C) 2026  Dante Bullock, Sovereign Logic

Intercepts LLM calls and tool executions at the interpreter level
via dynamic runtime patching. No framework modifications required.

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published
by the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

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
    # OpenAI
    "gpt-4o":              {"input": 2.50, "output": 10.00},
    "gpt-4o-2024-05-13":   {"input": 2.50, "output": 10.00},
    "gpt-4o-mini":         {"input": 0.15, "output": 0.60},
    "gpt-4-turbo":         {"input": 10.00, "output": 30.00},
    "gpt-4":               {"input": 30.00, "output": 60.00},
    # Anthropic
    "claude-3-5-sonnet":   {"input": 3.00, "output": 15.00},
    "claude-3-5-haiku":    {"input": 0.80, "output": 4.00},
    "claude-3-opus":       {"input": 15.00, "output": 75.00},
    "claude-3-sonnet":     {"input": 3.00, "output": 15.00},
    "claude-3-haiku":      {"input": 0.25, "output": 1.25},
    # Google
    "gemini-1.5-pro":      {"input": 1.25, "output": 5.00},
    "gemini-1.5-flash":    {"input": 0.075, "output": 0.30},
    "gemini-2.0-flash":    {"input": 0.10, "output": 0.40},
    # Default fallback
    "default":             {"input": 2.00, "output": 10.00},
}

# ──────────────────────────────────────────────
# 2. DATA STRUCTURES
# ──────────────────────────────────────────────

@dataclass
class ExecutionState:
    """Live telemetry state tracked entirely in-process."""
    total_cost: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    start_time: float = field(default_factory=time.time)
    total_tool_calls: int = 0
    # (tool_name, str(input)) → call_count — loop of death detector
    tool_history: Dict[Tuple[str, str], int] = field(default_factory=dict)


class ShackleInterrupt(Exception):
    """Raised when any circuit breaker trigger condition is met."""

    def __init__(
        self,
        message: str,
        trigger_type: str,
        state: ExecutionState,
        details: Dict[str, Any],
    ):
        super().__init__(message)
        self.trigger_type = trigger_type
        self.state = state
        self.details = details


# ──────────────────────────────────────────────
# 3. TRIGGER ENGINE (THE BRAIN)
# ──────────────────────────────────────────────

class TriggerEngine:
    """
    Evaluates every LLM call and tool execution against four circuit
    breaker conditions. Runs entirely client-side.
    """

    def __init__(
        self,
        budget: float = 0.20,
        max_repeat_calls: int = 3,
        timeout_seconds: float = 180.0,
        max_tool_calls: int = 50,
    ):
        self.budget = budget
        self.max_repeat_calls = max_repeat_calls
        self.timeout_seconds = timeout_seconds
        self.max_tool_calls = max_tool_calls

    # ── Trigger 2: Token / Dollar Budget ──────────────────────

    def evaluate_llm_call(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        state: ExecutionState,
    ) -> None:
        """Tracks spend and raises on budget breach."""
        pricing = MODEL_PRICING.get(
            model.lower(), MODEL_PRICING["default"]
        )
        call_cost = (
            (input_tokens * pricing["input"])
            + (output_tokens * pricing["output"])
        ) / 1_000_000

        state.total_cost += call_cost
        state.input_tokens += input_tokens
        state.output_tokens += output_tokens

        if state.total_cost >= self.budget:
            raise ShackleInterrupt(
                message=(
                    f"Budget breached: ${state.total_cost:.5f} spent "
                    f"(limit: ${self.budget:.2f})"
                ),
                trigger_type="BUDGET_EXCEEDED",
                state=state,
                details={
                    "model": model,
                    "current_cost": state.total_cost,
                    "limit": self.budget,
                    "input_tokens": state.input_tokens,
                    "output_tokens": state.output_tokens,
                },
            )

    # ── Trigger 1: Repeat Tool Call (Loop of Death) ───────────
    # ── Trigger 4: Execution Timeout ──────────────────────────

    def evaluate_tool_call(
        self,
        agent_name: str,
        tool_name: str,
        tool_input: Any,
        state: ExecutionState,
    ) -> None:
        """Detects infinite loops, error cascades, and timeouts."""

        elapsed = time.time() - state.start_time
        state.total_tool_calls += 1

        # TRIGGER 4: Wall-clock timeout
        if elapsed > self.timeout_seconds:
            raise ShackleInterrupt(
                message=(
                    f"Execution timeout: {elapsed:.1f}s elapsed "
                    f"(limit: {self.timeout_seconds}s)"
                ),
                trigger_type="TIMEOUT_REACHED",
                state=state,
                details={
                    "elapsed_seconds": elapsed,
                    "limit": self.timeout_seconds,
                },
            )

        # TRIGGER 1: Repeat tool call detection
        input_key = str(tool_input).strip()
        key = (tool_name, input_key)
        state.tool_history[key] = state.tool_history.get(key, 0) + 1
        count = state.tool_history[key]

        # Amplify sensitivity when input contains error signals
        input_lower = input_key.lower()
        is_error_loop = any(
            token in input_lower
            for token in ("error", "failed", "unauthorized", "401", "403", "500", "timeout")
        )
        effective_count = count + (1 if is_error_loop and count >= 2 else 0)

        if effective_count >= self.max_repeat_calls:
            raise ShackleInterrupt(
                message=(
                    f"Loop of Death detected: '{tool_name}' called "
                    f"{count}x with identical input"
                ),
                trigger_type="REPETITIVE_TOOL_CALL",
                state=state,
                details={
                    "agent": agent_name,
                    "tool": tool_name,
                    "input": input_key[:200],
                    "call_count": count,
                    "error_loop": is_error_loop,
                },
            )

        # TRIGGER: Max total tool calls
        if state.total_tool_calls >= self.max_tool_calls:
            raise ShackleInterrupt(
                message=(
                    f"Max tool calls reached: {state.total_tool_calls} "
                    f"(limit: {self.max_tool_calls})"
                ),
                trigger_type="MAX_TOOL_CALLS",
                state=state,
                details={
                    "total_calls": state.total_tool_calls,
                    "limit": self.max_tool_calls,
                },
            )


# ──────────────────────────────────────────────
# 4. HUMAN-IN-THE-LOOP TERMINAL INTERFACE
# ──────────────────────────────────────────────

def render_hitl_terminal(interrupt: ShackleInterrupt) -> str:
    """Renders an interactive CLI prompt when a circuit breaker trips."""

    TRIGGER_EMOJI = {
        "REPETITIVE_TOOL_CALL": "🔁",
        "BUDGET_EXCEEDED": "💰",
        "TIMEOUT_REACHED": "⏱️",
        "MAX_TOOL_CALLS": "📊",
    }

    emoji = TRIGGER_EMOJI.get(interrupt.trigger_type, "⚠️")

    # Build details block
    detail_lines = []
    for k, v in interrupt.details.items():
        label = k.replace("_", " ").title()
        detail_lines.append(f"[bold cyan]{label}:[/bold cyan] {v}")

    details_str = "\n".join(detail_lines)

    stats_str = (
        f"[bold gold3]Tokens:[/bold gold3] "
        f"In: {interrupt.state.input_tokens:,} | "
        f"Out: {interrupt.state.output_tokens:,}\n"
        f"[bold green]Session Cost:[/bold green] "
        f"${interrupt.state.total_cost:.5f}\n"
        f"[bold blue]Time Running:[/bold blue] "
        f"{time.time() - interrupt.state.start_time:.1f}s"
    )

    content = f"{details_str}\n\n[dim]─── Session Stats ───[/dim]\n{stats_str}"

    console.print()
    console.print(
        Panel(
            content,
            title=f"[bold red]{emoji} SHACKLE CIRCUIT BREAKER: "
                  f"{interrupt.trigger_type} {emoji}",
            border_style="red",
            expand=False,
        )
    )

    console.print("[bold yellow]Options:[/bold yellow]")
    console.print(
        "  [bold][R][/bold] Resume/Reset — clear history, continue execution"
    )
    console.print(
        "  [bold][S][/bold] Skip — return dummy output, attempt context flush"
    )
    console.print(
        "  [bold][A][/bold] Abort — hard terminate the current run"
    )
    console.print()

    valid = {"R", "S", "A"}
    while True:
        choice = input("Select action (R/S/A): ").strip().upper()
        if choice in valid:
            return choice
        console.print("[red]Invalid choice. Enter R, S, or A.[/red]")


# ──────────────────────────────────────────────
# 5. RUNTIME MONKEY-PATCHING (THE SHIM LAYER)
# ──────────────────────────────────────────────

_original_litellm_completion = None
_original_basetool_run = None
_original_crewai_execute_task = None


def _patch_litellm(engine: TriggerEngine, state: ExecutionState) -> None:
    """Intercept litellm.completion — covers CrewAI, AutoGen, LangChain."""
    global _original_litellm_completion

    try:
        import litellm

        if _original_litellm_completion is None:
            _original_litellm_completion = litellm.completion

        def patched_completion(*args: Any, **kwargs: Any) -> Any:
            response = _original_litellm_completion(*args, **kwargs)
            try:
                model = kwargs.get("model", "default")
                usage = response.get("usage", {}) if isinstance(response, dict) else {}
                # litellm normalizes usage across providers
                if hasattr(response, "usage"):
                    usage = response.usage
                    input_tok = getattr(usage, "prompt_tokens", 0) or 0
                    output_tok = getattr(usage, "completion_tokens", 0) or 0
                else:
                    input_tok = usage.get("prompt_tokens", 0)
                    output_tok = usage.get("completion_tokens", 0)

                engine.evaluate_llm_call(model, input_tok, output_tok, state)
            except ShackleInterrupt as si:
                action = render_hitl_terminal(si)
                if action == "A":
                    raise
                elif action == "R":
                    state.total_cost = 0.0
                    state.input_tokens = 0
                    state.output_tokens = 0
                # S: silently continue
            return response

        litellm.completion = patched_completion
        logger.info("SHACKLE: Hooked litellm.completion")

    except ImportError:
        logger.debug("litellm not available — skipping LLM hook")


def _patch_basetool(engine: TriggerEngine, state: ExecutionState) -> None:
    """Intercept langchain_core.tools.BaseTool.run — covers most tool calls."""
    global _original_basetool_run

    try:
        from langchain_core.tools import BaseTool

        if _original_basetool_run is None:
            _original_basetool_run = BaseTool.run

        def patched_run(self_: Any, *args: Any, **kwargs: Any) -> Any:
            tool_name = getattr(self_, "name", "unknown_tool")
            tool_input = args[0] if args else kwargs

            try:
                engine.evaluate_tool_call(
                    "Agent", tool_name, tool_input, state
                )
            except ShackleInterrupt as si:
                action = render_hitl_terminal(si)
                if action == "A":
                    raise
                elif action == "R":
                    state.tool_history.clear()
                elif action == "S":
                    return (
                        "[SHACKLE] Tool execution skipped by operator. "
                        "Proceed to next step."
                    )
            return _original_basetool_run(self_, *args, **kwargs)

        BaseTool.run = patched_run
        logger.info("SHACKLE: Hooked langchain_core.tools.BaseTool.run")

    except ImportError:
        logger.debug("langchain_core not available — skipping tool hook")


def _patch_crewai_agent(engine: TriggerEngine, state: ExecutionState) -> None:
    """
    Experimental: Hook CrewAI Agent.execute_task to catch internal
    reasoning loops that never surface a tool call (Manager Agent loops).
    """
    global _original_crewai_execute_task

    try:
        from crewai.agent import Agent

        if _original_crewai_execute_task is None:
            _original_crewai_execute_task = Agent.execute_task

        def patched_execute_task(self_: Any, *args: Any, **kwargs: Any) -> Any:
            agent_name = getattr(self_, "role", "UnknownAgent")
            task_desc = str(args[0])[:200] if args else "planning"

            try:
                engine.evaluate_tool_call(
                    agent_name, "internal_reasoning", task_desc, state
                )
            except ShackleInterrupt as si:
                action = render_hitl_terminal(si)
                if action == "A":
                    raise
                elif action == "R":
                    state.tool_history.clear()
                elif action == "S":
                    return {
                        "status": "skipped",
                        "output": "Task bypassed by SHACKLE circuit breaker.",
                    }

            return _original_crewai_execute_task(self_, *args, **kwargs)

        Agent.execute_task = patched_execute_task
        setattr(Agent, "_shackle_patched", True)
        logger.info(
            "SHACKLE: Hooked CrewAI Agent.execute_task "
            "(Manager loop protection — experimental)"
        )

    except ImportError:
        logger.debug("crewai not available — skipping Agent hook")


def _apply_patches(engine: TriggerEngine, state: ExecutionState) -> None:
    """Apply all available runtime patches."""
    _patch_litellm(engine, state)
    _patch_basetool(engine, state)
    _patch_crewai_agent(engine, state)


def _remove_patches() -> None:
    """Restore all original functions."""
    global _original_litellm_completion, _original_basetool_run
    global _original_crewai_execute_task

    if _original_litellm_completion is not None:
        try:
            import litellm
            litellm.completion = _original_litellm_completion
        except ImportError:
            pass
        _original_litellm_completion = None

    if _original_basetool_run is not None:
        try:
            from langchain_core.tools import BaseTool
            BaseTool.run = _original_basetool_run
        except ImportError:
            pass
        _original_basetool_run = None

    if _original_crewai_execute_task is not None:
        try:
            from crewai.agent import Agent
            Agent.execute_task = _original_crewai_execute_task
        except ImportError:
            pass
        _original_crewai_execute_task = None


# ──────────────────────────────────────────────
# 6. PUBLIC API — THE GUARD DECORATOR
# ──────────────────────────────────────────────

class Guard:
    """
    One-line circuit breaker for autonomous agent workflows.

    Usage::

        from shackle import Guard

        @Guard(budget=0.25, max_repeat_calls=3, timeout_seconds=180)
        def run_agents():
            my_crew.kickoff()

        run_agents()
    """

    def __init__(
        self,
        budget: float = 0.20,
        max_repeat_calls: int = 3,
        timeout_seconds: float = 180.0,
        max_tool_calls: int = 50,
    ):
        self.engine = TriggerEngine(
            budget=budget,
            max_repeat_calls=max_repeat_calls,
            timeout_seconds=timeout_seconds,
            max_tool_calls=max_tool_calls,
        )

    def __call__(self, func: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            state = ExecutionState()
            _apply_patches(self.engine, state)

            try:
                console.print(
                    "\n[bold dim]⛓️  SHACKLE armed[/bold dim] — "
                    f"budget: [green]${self.engine.budget:.2f}[/green] | "
                    f"repeat limit: [yellow]{self.engine.max_repeat_calls}x[/yellow] | "
                    f"timeout: [blue]{self.engine.timeout_seconds}s[/blue]"
                )
                result = func(*args, **kwargs)
                return result
            except ShackleInterrupt as si:
                console.print(
                    f"\n[bold red]⛓️ SHACKLE: Execution aborted — "
                    f"{si.trigger_type}[/bold red]"
                )
                raise
            finally:
                _remove_patches()
                console.print(
                    f"\n[bold green]⛓️  SHACKLE SESSION COMPLETE[/bold green] — "
                    f"Spent [bold]${state.total_cost:.5f}[/bold] | "
                    f"Tokens in: [bold]{state.input_tokens:,}[/bold] | "
                    f"out: [bold]{state.output_tokens:,}[/bold] | "
                    f"Tool calls: {state.total_tool_calls}"
                )

        # Preserve original function metadata
        wrapper.__name__ = func.__name__
        wrapper.__doc__ = func.__doc__
        wrapper.__wrapped__ = func

        return wrapper
