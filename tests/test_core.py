"""Unit tests for SHACKLE core engine — no external dependencies required."""

import time
import pytest
from shackle.core import (
    TriggerEngine,
    ExecutionState,
    ShackleInterrupt,
    Guard,
    MODEL_PRICING,
)


class TestPricingTable:
    def test_known_models_have_pricing(self):
        assert "gpt-4o" in MODEL_PRICING
        assert "claude-3-5-sonnet" in MODEL_PRICING
        assert "gemini-1.5-pro" in MODEL_PRICING

    def test_default_fallback_exists(self):
        assert "default" in MODEL_PRICING

    def test_pricing_values_are_positive(self):
        for model, pricing in MODEL_PRICING.items():
            assert pricing["input"] >= 0
            assert pricing["output"] >= 0


class TestExecutionState:
    def test_initial_values(self):
        state = ExecutionState()
        assert state.total_cost == 0.0
        assert state.input_tokens == 0
        assert state.output_tokens == 0
        assert state.total_tool_calls == 0
        assert state.tool_history == {}

    def test_start_time_is_now(self):
        before = time.time()
        state = ExecutionState()
        after = time.time()
        assert before <= state.start_time <= after


class TestTriggerEngineLLM:
    def test_budget_tracking(self):
        engine = TriggerEngine(budget=1.00)
        state = ExecutionState()
        # 1M input + 500K output at gpt-4o-mini = very cheap
        engine.evaluate_llm_call("gpt-4o-mini", 1_000_000, 500_000, state)
        assert state.total_cost > 0
        assert state.input_tokens == 1_000_000
        assert state.output_tokens == 500_000

    def test_budget_breach_raises(self):
        engine = TriggerEngine(budget=0.0001)  # impossibly low
        state = ExecutionState()
        with pytest.raises(ShackleInterrupt) as exc:
            engine.evaluate_llm_call("gpt-4o", 100_000, 100_000, state)
        # call cost (1.25) > remaining (0.0001) -- this is an OVERRUN
        # (a call that would push us over), not an EXCEEDED (we already
        # went over). decide() must catch it BEFORE state is mutated.
        assert exc.value.trigger_type == "BUDGET_OVERRUN"
        # And the state must NOT have been mutated by the denied call.
        assert state.total_cost == 0.0
        assert state.input_tokens == 0
        assert state.output_tokens == 0

    def test_budget_exhausted_raises(self):
        """When remaining is already 0 (a prior call spent the last dollar),
        any further call is denied with BUDGET_EXCEEDED, not BUDGET_OVERRUN."""
        engine = TriggerEngine(budget=0.20)
        state = ExecutionState()
        # Pre-populate state as if a prior call already exhausted the budget.
        state.total_cost = 0.20
        with pytest.raises(ShackleInterrupt) as exc:
            engine.evaluate_llm_call("gpt-4o", 1_000, 1_000, state)
        assert exc.value.trigger_type == "BUDGET_EXCEEDED"

    def test_budget_exact_hit_raises(self):
        """A call that exactly exhausts the budget (remaining - cost == 0)
        is ALLOWED by decide() (not an overrun) but trips the post-mutation
        hard limit, raising BUDGET_EXCEEDED."""
        engine = TriggerEngine(budget=0.20)
        state = ExecutionState()
        # gpt-4o-mini: $0.15/1M in, $0.60/1M out. 25,000 output tokens
        # at $0.60/1M = $0.015 EXACTLY in floating point. Pre-charge state
        # to $0.185 so pre_remaining = $0.015 exactly, and the call lands
        # precisely on $0.20 total (not above, not below).
        state.total_cost = 0.185
        with pytest.raises(ShackleInterrupt) as exc:
            engine.evaluate_llm_call("gpt-4o-mini", 0, 25_000, state)
        assert exc.value.trigger_type == "BUDGET_EXCEEDED"

    def test_concurrent_budget_no_overrun(self):
        """Concurrent LLM calls must not allow the budget to be exceeded
        due to lost updates in the read-modify-write of total_cost.

        This is the runtime counterpart to the conformance fixture
        ``concurrent_budget_overrun``. Without the per-state RLock around
        the read-decide-mutate-check critical section, this test is flaky
        and total_cost can exceed budget.
        """
        import threading
        engine = TriggerEngine(budget=0.20)
        state = ExecutionState()
        # Each call costs $0.05 at gpt-4o pricing: 4000 in + 4000 out
        # -> (4000*2.50 + 4000*10.00)/1M = 0.05 exactly.
        N_THREADS = 20
        results: list = []
        barrier = threading.Barrier(N_THREADS)

        def worker():
            barrier.wait()  # release all threads simultaneously
            try:
                engine.evaluate_llm_call("gpt-4o", 4_000, 4_000, state)
                results.append(("ok", None))
            except ShackleInterrupt as e:
                results.append(("deny", e.trigger_type))

        threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Invariant 1: total cost must never exceed budget.
        assert state.total_cost <= engine.budget + 1e-9, (
            f"Budget overrun: total_cost={state.total_cost} > budget={engine.budget}"
        )
        # Invariant 2: at most 4 calls (4 * 0.05 = 0.20 = budget) succeeded.
        ok = [r for r in results if r[0] == "ok"]
        assert len(ok) <= 4, f"Too many successful calls under contention: {len(ok)}"
        # Invariant 3: state total_cost must equal (successful calls) * 0.05,
        # i.e. no lost updates: the counter must be the EXACT sum of
        # individual call_costs, not a clobbered-over write.
        assert abs(state.total_cost - len(ok) * 0.05) < 1e-9, (
            f"Lost update: total_cost={state.total_cost} != {len(ok) * 0.05}"
        )
        # Invariant 4: every denial must be a budget-family trigger.
        for status, trig in (r for r in results if r[0] == "deny"):
            assert trig in ("BUDGET_EXCEEDED", "BUDGET_OVERRUN"), (
                f"Unexpected denial trigger under concurrent budget contention: {trig}"
            )

    def test_unknown_model_uses_default_pricing(self):
        engine = TriggerEngine(budget=1.00)
        state = ExecutionState()
        engine.evaluate_llm_call("nonexistent-model-42", 1_000, 1_000, state)
        assert state.total_cost > 0


class TestTriggerEngineToolCalls:
    def test_repeat_call_detection(self):
        engine = TriggerEngine(max_repeat_calls=3)
        state = ExecutionState()

        # First 2 calls: no interrupt
        engine.evaluate_tool_call("Agent", "search", "latest news", state)
        engine.evaluate_tool_call("Agent", "search", "latest news", state)
        assert state.tool_history[("search", "latest news")] == 2

        # 3rd call with same input: should trip
        with pytest.raises(ShackleInterrupt) as exc:
            engine.evaluate_tool_call("Agent", "search", "latest news", state)
        assert exc.value.trigger_type == "REPETITIVE_TOOL_CALL"

    def test_different_inputs_no_trigger(self):
        engine = TriggerEngine(max_repeat_calls=3)
        state = ExecutionState()

        engine.evaluate_tool_call("Agent", "search", "query A", state)
        engine.evaluate_tool_call("Agent", "search", "query B", state)
        engine.evaluate_tool_call("Agent", "search", "query C", state)
        # No exception — different inputs, no repeat detection

    def test_error_amplification_triggers_earlier(self):
        """Error strings should amplify sensitivity, tripping at count 2."""
        engine = TriggerEngine(max_repeat_calls=3)
        state = ExecutionState()

        engine.evaluate_tool_call("Agent", "api", "401 Unauthorized", state)
        # Second call with error string: should trip even though max_repeat_calls=3
        with pytest.raises(ShackleInterrupt) as exc:
            engine.evaluate_tool_call("Agent", "api", "401 Unauthorized", state)
        assert exc.value.trigger_type == "REPETITIVE_TOOL_CALL"
        assert exc.value.details["error_loop"] is True

    def test_timeout_detection(self):
        engine = TriggerEngine(timeout_seconds=0.001)  # 1ms timeout
        state = ExecutionState()
        time.sleep(0.01)  # ensure we're past timeout
        with pytest.raises(ShackleInterrupt) as exc:
            engine.evaluate_tool_call("Agent", "slow_tool", "input", state)
        assert exc.value.trigger_type == "TIMEOUT_REACHED"

    def test_max_tool_calls(self):
        engine = TriggerEngine(max_tool_calls=3)
        state = ExecutionState()

        engine.evaluate_tool_call("Agent", "t1", "a", state)
        engine.evaluate_tool_call("Agent", "t2", "b", state)
        # 3rd call hits limit (total_tool_calls goes 1→2→3)
        with pytest.raises(ShackleInterrupt) as exc:
            engine.evaluate_tool_call("Agent", "t3", "c", state)
        assert exc.value.trigger_type == "MAX_TOOL_CALLS"


class TestGuardDecorator:
    def test_guard_preserves_return_value(self):
        """Guard should pass through the return value of the wrapped function."""
        @Guard(budget=100.0, max_repeat_calls=10)
        def my_func():
            return "success"

        result = my_func()
        assert result == "success"

    def test_guard_preserves_function_metadata(self):
        @Guard(budget=100.0)
        def documented_func():
            """This function has a docstring."""
            pass

        assert documented_func.__name__ == "documented_func"
        assert documented_func.__doc__ == "This function has a docstring."

    def test_guard_session_summary(self):
        """Guard should print a session summary after execution."""
        @Guard(budget=100.0)
        def simple():
            return 42

        result = simple()
        assert result == 42


class TestShackleInterrupt:
    def test_interrupt_is_exception(self):
        state = ExecutionState()
        si = ShackleInterrupt(
            message="test",
            trigger_type="TEST_TRIGGER",
            state=state,
            details={"key": "value"},
        )
        assert isinstance(si, Exception)
        assert si.trigger_type == "TEST_TRIGGER"
        assert si.details["key"] == "value"

    def test_interrupt_can_be_caught(self):
        state = ExecutionState()
        try:
            raise ShackleInterrupt("msg", "TYPE", state, {})
        except ShackleInterrupt as e:
            assert str(e) == "msg"
