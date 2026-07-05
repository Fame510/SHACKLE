#!/usr/bin/env python3
"""
Unit tests for the atomic SHACKLE state decision path (evaluate_and_record).

Runs against a REAL Redis instance (CI service, or local via REDIS_URL). Verifies
the TOCTOU fix: budget + repeat evaluation and call recording happen in a single
atomic Redis operation.

Run:
    REDIS_URL=redis://localhost:6379/0 pytest v2/daemon/test_state_atomic.py -q
"""

import asyncio
import os
import uuid

import pytest
import pytest_asyncio

from state import StateManager


REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


@pytest_asyncio.fixture
async def state():
    sm = StateManager(REDIS_URL)
    await sm.connect()
    yield sm
    await sm.close()


def _new_session():
    return f"test_{uuid.uuid4().hex[:12]}"


@pytest.mark.asyncio
async def test_allow_under_budget(state):
    session = _new_session()
    res = await state.evaluate_and_record(
        session_id=session, tool_name="tool_a",
        parameters={"x": 1}, estimated_cost=0.001,
    )
    assert res["decision"] == "ALLOW"
    await state.clear_session(session)


@pytest.mark.asyncio
async def test_deny_over_budget(state):
    session = _new_session()
    res = await state.evaluate_and_record(
        session_id=session, tool_name="expensive",
        parameters={"size": "large"}, estimated_cost=100.0,
    )
    assert res["decision"] == "DENY"
    await state.clear_session(session)


@pytest.mark.asyncio
async def test_deny_does_not_record(state):
    session = _new_session()
    await state.evaluate_and_record(
        session_id=session, tool_name="expensive",
        parameters={"size": "large"}, estimated_cost=100.0,
    )
    count = await state.get_repeat_count(session, "expensive", {"size": "large"})
    assert count == 0
    await state.clear_session(session)


@pytest.mark.asyncio
async def test_hitl_after_repeats(state):
    session = _new_session()
    params = {"iteration": "same"}
    decisions = []
    for _ in range(6):
        res = await state.evaluate_and_record(
            session_id=session, tool_name="repeat_tool",
            parameters=params, estimated_cost=0.001, max_repeat=3,
        )
        decisions.append(res["decision"])
    assert decisions[0] == "ALLOW"
    assert "HITL" in decisions
    await state.clear_session(session)


@pytest.mark.asyncio
async def test_concurrent_calls_are_atomic(state):
    """
    Fire many identical calls concurrently. The atomic Lua path must ensure the
    number of recorded (ALLOWed) calls equals the number of ALLOW verdicts, and
    that concurrency cannot allow all of them -- the repeat ceiling still bites.
    """
    session = _new_session()
    params = {"race": "yes"}
    max_repeat = 3

    async def one():
        return await state.evaluate_and_record(
            session_id=session, tool_name="race_tool",
            parameters=params, estimated_cost=0.001, max_repeat=max_repeat,
        )

    results = await asyncio.gather(*[one() for _ in range(20)])
    allows = sum(1 for r in results if r["decision"] == "ALLOW")
    hitls = sum(1 for r in results if r["decision"] == "HITL")

    # Core atomicity guarantee: recorded count == number of ALLOW verdicts
    # (no lost or phantom records under concurrency).
    recorded = await state.get_repeat_count(session, "race_tool", params)
    assert recorded == allows
    # The ceiling must engage: not all 20 concurrent calls can be allowed.
    assert hitls > 0
    assert allows < 20
    await state.clear_session(session)
