"""
SHACKLE SP/1.0 - LiteLLM Guardrail.
Copyright (C) 2026  Dante Bullock, Sovereign Logic.  AGPL-3.0-or-later.

This module provides two LiteLLM guardrails, both enforcing the SHACKLE SP/1.0
decision surface, so LiteLLM-powered agents (CrewAI, AutoGen, LangGraph, custom)
get SHACKLE protection with one integration.

  * ShackleGuardrail          - Option A: pure reference decide() from
                                shackle.conformance. Stateless w.r.t. the V2
                                daemon; verdicts are SP/1.0-exact by construction.
  * ShackleEngineGuardrail    - Option B: drives shackle.core.TriggerEngine with a
                                per-instance ExecutionState for stateful budget /
                                repeat / timeout enforcement, and surfaces
                                ShackleInterrupt as a block.

Both raise ShackleBlocked when a call must be stopped. In a proxy there is no
interactive human, so HITL/DENY both fail closed (block). Interactive terminal
HITL remains available through shackle.core.Guard for local SDK runs.

Usage (SDK, Option A):
    from shackle.litellm_shackle_guardrail import ShackleGuardrail
    guard = ShackleGuardrail(budget_usd=0.50, max_repeat_calls=3)
    guard.check({"model": "gpt-4o", "messages": [...]})   # raises on DENY/HITL
    # ... make the call ...
    guard.record({"model": "gpt-4o"}, response)            # track spend

Usage (SDK, Option B - stateful engine):
    from shackle.litellm_shackle_guardrail import ShackleEngineGuardrail
    guard = ShackleEngineGuardrail(budget=0.50, max_repeat_calls=3)
    guard.check({"model": "gpt-4o", "messages": [...]})
    guard.record({"model": "gpt-4o"}, response)
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from shackle.conformance import decide, canonical_hash
from shackle.core import (
    TriggerEngine,
    ExecutionState,
    ShackleInterrupt,
    _extract_llm_usage,
)

try:
    from litellm.integrations.custom_guardrail import CustomGuardrail
except Exception:  # pragma: no cover - import shim so tests run without litellm
    class CustomGuardrail:  # type: ignore
        def __init__(self, **kwargs: Any) -> None:
            pass


# Conservative default per-1k-token prices (USD) for Option A budget tracking.
DEFAULT_PRICE = {"prompt": 0.005, "completion": 0.015}


class ShackleBlocked(Exception):
    """Raised when SHACKLE blocks an LLM call (verdict DENY or HITL)."""

    def __init__(self, verdict: str, reason: str, model: Optional[str] = None) -> None:
        self.verdict = verdict
        self.reason = reason
        self.model = model
        suffix = f" (model={model})" if model else ""
        super().__init__(f"SHACKLE {verdict}: {reason}{suffix}")


def _usage_tokens(response: Any) -> "tuple[int, int]":
    """Best-effort (prompt_tokens, completion_tokens) from a LiteLLM response."""
    try:
        return _extract_llm_usage(response)
    except Exception:
        pass
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if not usage:
        return 0, 0
    pt = getattr(usage, "prompt_tokens", None)
    ct = getattr(usage, "completion_tokens", None)
    if pt is None and isinstance(usage, dict):
        pt = usage.get("prompt_tokens", 0)
        ct = usage.get("completion_tokens", 0)
    return int(pt or 0), int(ct or 0)


# ══════════════════════════════════════════════════════════════════════
# Option A - pure conformance.decide()
# ══════════════════════════════════════════════════════════════════════
class ShackleGuardrail(CustomGuardrail):
    """SP/1.0 guardrail backed directly by conformance.decide()."""

    def __init__(
        self,
        budget_usd: float = 0.25,
        max_repeat_calls: int = 3,
        hitl_mode: str = "never",
        hitl_budget_threshold: Optional[float] = None,
        price_per_1k: Optional[Dict[str, Dict[str, float]]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.config: Dict[str, Any] = {
            "budget_usd": budget_usd,
            "max_repeat_calls": max_repeat_calls,
            "hitl_mode": hitl_mode,
            "hitl_budget_threshold": hitl_budget_threshold,
        }
        self.price_per_1k = price_per_1k or {}
        self.state: Dict[str, Any] = {
            "circuit_tripped": False,
            "seen_nonces": [],
            "budget_initial_usd": budget_usd,
            "budget_remaining_usd": budget_usd,
            "repeat_counts": {},
            "last_tool_name": None,
            "pending_transition": None,
        }

    def _price(self, model: str) -> Dict[str, float]:
        return self.price_per_1k.get(model, DEFAULT_PRICE)

    def _build_call(self, request_data: Dict[str, Any]) -> Dict[str, Any]:
        model = request_data.get("model", "unknown")
        params = {
            "model": model,
            "messages": request_data.get("messages", []) or [],
            "tools": request_data.get("tools", []) or request_data.get("functions", []) or [],
        }
        return {"tool_name": f"llm:{model}", "params": params, "nonce": canonical_hash(params)}

    def _evaluate(self, request_data: Dict[str, Any]) -> None:
        call = self._build_call(request_data)
        verdict, reason = decide(self.config, self.state, call)
        if verdict == "ALLOW":
            tool = call["tool_name"]
            self.state["last_tool_name"] = tool
            self.state["repeat_counts"][tool] = self.state["repeat_counts"].get(tool, 0) + 1
            self.state["seen_nonces"].append(call["nonce"])
            return
        if verdict == "DENY":
            self.state["circuit_tripped"] = True
        raise ShackleBlocked(verdict, reason, request_data.get("model"))

    def _record(self, request_data: Dict[str, Any], response: Any) -> None:
        pt, ct = _usage_tokens(response)
        price = self._price(request_data.get("model", "unknown"))
        cost = (pt / 1000.0) * price["prompt"] + (ct / 1000.0) * price["completion"]
        self.state["budget_remaining_usd"] -= cost

    # SDK convenience
    def check(self, request_data: Dict[str, Any]) -> None:
        self._evaluate(request_data)

    def record(self, request_data: Dict[str, Any], response: Any) -> None:
        self._record(request_data, response)

    # LiteLLM proxy hooks
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):  # noqa: ANN001
        self._evaluate(data)
        return data

    async def async_post_call_success_hook(self, user_api_key_dict, response, data, call_type=None):  # noqa: ANN001
        self._record(data, response)
        return response


# ══════════════════════════════════════════════════════════════════════
# Option B - stateful shackle.core.TriggerEngine
# ══════════════════════════════════════════════════════════════════════
class ShackleEngineGuardrail(CustomGuardrail):
    """
    SP/1.0 guardrail driving the full TriggerEngine + ExecutionState.

    Each LLM request is evaluated as a tool call (for repeat/timeout/tool-count
    limits) and its post-call token usage is charged via evaluate_llm_call (for
    budget). A ShackleInterrupt from the engine is surfaced as ShackleBlocked.
    Reentrant per instance: state lives on the instance, not module globals.
    """

    def __init__(
        self,
        budget: float = 0.25,
        max_repeat_calls: int = 3,
        timeout_seconds: float = 180.0,
        max_tool_calls: int = 50,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.engine = TriggerEngine(
            budget=budget,
            max_repeat_calls=max_repeat_calls,
            timeout_seconds=timeout_seconds,
            max_tool_calls=max_tool_calls,
        )
        self.state = ExecutionState()

    def _tool_input(self, request_data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "model": request_data.get("model", "unknown"),
            "messages": request_data.get("messages", []) or [],
            "tools": request_data.get("tools", []) or request_data.get("functions", []) or [],
        }

    def _evaluate(self, request_data: Dict[str, Any]) -> None:
        model = request_data.get("model", "unknown")
        try:
            self.engine.evaluate_tool_call(
                agent_name="litellm",
                tool_name=f"llm:{model}",
                tool_input=self._tool_input(request_data),
                state=self.state,
            )
        except ShackleInterrupt as si:
            raise ShackleBlocked(
                verdict=self.state.last_decision[0],
                reason=si.trigger_type,
                model=model,
            ) from si

    def _record(self, request_data: Dict[str, Any], response: Any) -> None:
        model = request_data.get("model", "unknown")
        pt, ct = _usage_tokens(response)
        try:
            self.engine.evaluate_llm_call(
                model=model, input_tokens=pt, output_tokens=ct, state=self.state,
            )
        except ShackleInterrupt as si:
            raise ShackleBlocked(
                verdict=self.state.last_decision[0],
                reason=si.trigger_type,
                model=model,
            ) from si

    # SDK convenience
    def check(self, request_data: Dict[str, Any]) -> None:
        self._evaluate(request_data)

    def record(self, request_data: Dict[str, Any], response: Any) -> None:
        self._record(request_data, response)

    # LiteLLM proxy hooks
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):  # noqa: ANN001
        self._evaluate(data)
        return data

    async def async_post_call_success_hook(self, user_api_key_dict, response, data, call_type=None):  # noqa: ANN001
        self._record(data, response)
        return response
