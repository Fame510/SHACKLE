"""Unit tests for the SHACKLE AutoGen wrapper (real TriggerEngine, no autogen needed)."""
import asyncio

import pytest

from shackle.autogen_shackle_wrapper import (
    wrap_tool,
    create_shackle_agent,
    ShackleBlocked,
    _AUTOGEN,
)


def test_wrap_tool_allows_then_blocks_on_repeat():
    calls = {"n": 0}

    @wrap_tool(budget=100.0, max_repeat_calls=2, timeout_seconds=1000)
    def search(q):
        calls["n"] += 1
        return f"result:{q}"

    assert search("same") == "result:same"   # count 1 -> ok
    with pytest.raises(ShackleBlocked) as e:
        search("same")                        # count 2 >= max -> trip
    assert e.value.trigger_type == "REPETITIVE_TOOL_CALL"
    assert calls["n"] == 1                    # blocked call never ran the body


def test_wrap_tool_distinct_inputs_pass():
    @wrap_tool(budget=100.0, max_repeat_calls=3, timeout_seconds=1000)
    def tool(x):
        return x

    assert tool("a") == "a"
    assert tool("b") == "b"
    assert tool("c") == "c"   # distinct canonical inputs, no trip


def test_wrap_tool_bare_decorator_form():
    @wrap_tool
    def tool(x):
        return x * 2

    assert tool(3) == 6


def test_wrap_tool_async():
    @wrap_tool(budget=100.0, max_repeat_calls=2, timeout_seconds=1000)
    async def afetch(u):
        return f"got:{u}"

    async def run():
        first = await afetch("same")
        assert first == "got:same"
        with pytest.raises(ShackleBlocked):
            await afetch("same")

    asyncio.run(run())


def test_create_shackle_agent_without_autogen_raises():
    if _AUTOGEN:
        pytest.skip("autogen installed; skip the not-installed path")
    with pytest.raises(RuntimeError):
        create_shackle_agent(name="x")
