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

import pytest_asyncio

import pytest

# Integration test: exercises the atomic decision path against a REAL Redis.
# Skip cleanly (instead of erroring at collection) when redis-py is not
# installed, so a fresh clone's `pytest` run stays green. To actually run it,
# install redis and set REDIS_URL (see module docstring).
pytest.importorskip("redis", reason="requires redis-py and a live Redis (set REDIS_URL)")

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


def _request_id():
    return uuid.uuid4().hex + uuid.uuid4().hex[:16]


@pytest.mark.asyncio
async def test_allow_under_budget(state):
    session = _new_session()
    res = await state.evaluate_and_record(
        session_id=session, tool_name="tool_a",
        parameters={"x": 1}, estimated_cost=0.001, request_id=_request_id(),
    )
    assert res["decision"] == "ALLOW"
    await state.clear_session(session)


@pytest.mark.asyncio
async def test_deny_over_budget(state):
    session = _new_session()
    res = await state.evaluate_and_record(
        session_id=session, tool_name="expensive",
        parameters={"size": "large"}, estimated_cost=100.0, request_id=_request_id(),
    )
    assert res["decision"] == "DENY"
    await state.clear_session(session)


@pytest.mark.asyncio
async def test_deny_does_not_record(state):
    session = _new_session()
    await state.evaluate_and_record(
        session_id=session, tool_name="expensive",
        parameters={"size": "large"}, estimated_cost=100.0, request_id=_request_id(),
    )
    count = await state.get_repeat_count(session, "expensive", {"size": "large"})
    assert count == 0
    await state.clear_session(session)


@pytest.mark.asyncio
async def test_deny_at_repeat_ceiling(state):
    """
    SP/1.0 conformance ('deny_max_repeat'): the repeat ceiling trips at exactly
    max_repeat total attempts. With max_repeat=3, the effective count (prior + the
    current call) reaches 3 on the third identical call, which is DENIED with
    reason max_repeat_exceeded. So the first two are ALLOWed, the rest DENIED.
    (DENY is stricter than HITL; enforcement is not weakened.)
    """
    session = _new_session()
    params = {"iteration": "same"}
    decisions = []
    reasons = []
    for _ in range(6):
        res = await state.evaluate_and_record(
            session_id=session, tool_name="repeat_tool",
            parameters=params, estimated_cost=0.001, max_repeat=3, request_id=_request_id(),
        )
        decisions.append(res["decision"])
        reasons.append(res.get("reason"))
    assert decisions[:2] == ["ALLOW", "ALLOW"]
    assert all(d == "DENY" for d in decisions[2:])
    assert "max_repeat_exceeded" in reasons
    await state.clear_session(session)


@pytest.mark.asyncio
async def test_replayed_request_id_is_denied_without_second_reservation(state):
    session = _new_session()
    req = _request_id()
    first = await state.evaluate_and_record(
        session_id=session, tool_name="replay_tool", parameters={"x": 1},
        estimated_cost=0.25, request_id=req,
    )
    second = await state.evaluate_and_record(
        session_id=session, tool_name="replay_tool", parameters={"x": 1},
        estimated_cost=0.25, request_id=req,
    )
    assert first["decision"] == "ALLOW"
    assert second["decision"] == "DENY"
    assert second["reason"] == "fail_closed:request_replay"
    assert await state.get_repeat_count(session, "replay_tool", {"x": 1}) == 1
    await state.clear_session(session)


@pytest.mark.asyncio
async def test_postexec_requires_matching_single_use_capability(state):
    session = _new_session()
    req = _request_id()
    params = {"recipient": "verified"}
    decision = await state.evaluate_and_record(
        session_id=session, tool_name="send", parameters=params,
        estimated_cost=0.25, request_id=req,
    )
    assert decision["decision"] == "ALLOW"
    assert not await state.record_post_exec_once(session, _request_id(), "send", params, 0.25)
    assert not await state.record_post_exec_once(session, req, "send", {"recipient": "tampered"}, 0.25)
    assert await state.record_post_exec_once(session, req, "send", params, 0.30)
    assert not await state.record_post_exec_once(session, req, "send", params, 0.30)
    status = await state.get_budget_status(session)
    assert status["spent"] == pytest.approx(0.30)
    await state.clear_session(session)


@pytest.mark.asyncio
async def test_hitl_approval_reuses_existing_budget_reservation(state):
    session = _new_session()
    request_id = _request_id()
    params = {"context": "opaque"}
    result = await state.evaluate_and_record(
        session_id=session, tool_name="tool_requires_review", parameters=params,
        estimated_cost=0.50, request_id=request_id,
    )
    assert result["decision"] == "HITL"
    reserved_key = f"{state._budget_key(session)}:reserved"
    assert float(await state.redis.get(reserved_key)) == pytest.approx(0.50)
    # Approval creates the one-shot execution capability but must not count the
    # same estimate twice against the budget.
    assert await state.grant_hitl_execution(session, request_id)
    assert float(await state.redis.get(reserved_key)) == pytest.approx(0.50)
    assert await state.record_post_exec_once(session, request_id, "tool_requires_review", params, 0.50)
    assert (await state.get_budget_status(session))["spent"] == pytest.approx(0.50)
    await state.clear_session(session)
    await state.redis.delete(reserved_key)


@pytest.mark.asyncio
async def test_concurrent_different_calls_cannot_overspend_reserved_budget(state):
    session = _new_session()
    await state.set_budget_limit(session, 1.0)

    async def one(i):
        return await state.evaluate_and_record(
            session_id=session, tool_name=f"distinct-{i}", parameters={"i": i},
            estimated_cost=0.75, default_limit=1.0, request_id=_request_id(),
        )

    results = await asyncio.gather(*(one(i) for i in range(20)))
    assert sum(r["decision"] == "ALLOW" for r in results) == 1
    assert sum(r["decision"] == "DENY" for r in results) == 19
    reserved = float(await state.redis.get(f"{state._budget_key(session)}:reserved"))
    assert reserved == pytest.approx(0.75)
    await state.clear_session(session)
    await state.redis.delete(f"{state._budget_key(session)}:reserved")


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
            parameters=params, estimated_cost=0.001, max_repeat=max_repeat, request_id=_request_id(),
        )

    results = await asyncio.gather(*[one() for _ in range(20)])
    allows = sum(1 for r in results if r["decision"] == "ALLOW")
    denies = sum(1 for r in results if r["decision"] == "DENY")

    # Core atomicity guarantee: the recorded history depth equals the number of
    # ALLOW verdicts -- no lost or phantom records under concurrency, and DENIED
    # calls never mutate state.
    recorded = await state.get_repeat_count(session, "race_tool", params)
    assert recorded == allows
    # The ceiling engages atomically: with max_repeat=3 exactly (max_repeat - 1)
    # identical calls may be allowed+recorded; the rest are denied. No TOCTOU
    # leak lets extra calls slip past the ceiling under concurrency.
    assert allows == max_repeat - 1
    assert denies == 20 - (max_repeat - 1)
    await state.clear_session(session)
