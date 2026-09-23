"""Locally reproducible hostile-boundary tests for SHACKLE hardening.

These tests exercise Python/runtime boundaries in-process. They do not model a
hostile OS, kernel, hypervisor, or arbitrary code already executing in-process.
"""
import asyncio
import hashlib
import importlib.util
import json
import math
import os
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
DAEMON_DIR = ROOT / "v2" / "daemon"
if str(DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(DAEMON_DIR))

spec = importlib.util.spec_from_file_location("shackle_test_daemon", DAEMON_DIR / "daemon.py")
daemon = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = daemon
spec.loader.exec_module(daemon)

from client import ShackleClient, shackled
from state import StateManager
from decision import decide_for_daemon
from shackle.conformance import decide, vector_hash


class HostileList(list):
    def __getitem__(self, item):
        raise AssertionError("hostile container method executed")


class BombEstimate:
    def __call__(self, *args, **kwargs):
        raise RuntimeError("estimator unavailable")


class StubResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.payload = payload or {}

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("upstream refused")


class StubClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = []

    async def get(self, *args, **kwargs):
        return self.responses.pop(0)

    async def post(self, path, **kwargs):
        self.posts.append((path, kwargs))
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def reset_hitl_globals():
    daemon.hitl_pending.clear()
    daemon.hitl_wait_claimed.clear()
    daemon.hitl_created_at.clear()
    daemon.hitl_bindings.clear()
    daemon.websocket_connections.clear()
    yield
    daemon.hitl_pending.clear()
    daemon.hitl_wait_claimed.clear()
    daemon.hitl_created_at.clear()
    daemon.hitl_bindings.clear()
    daemon.websocket_connections.clear()


def test_all_profile_fixture_hashes_and_vector_seals_are_pinned():
    manifest_path = ROOT / "fixtures/certification-profiles.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["schema"] == "shackle-certification-profile-manifest-v1"
    for profile in manifest["profiles"]:
        fixture_path = ROOT / profile["fixture"]
        raw = fixture_path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == profile["fixture_sha256"], profile["id"]
        assert len(raw) == profile["fixture_bytes"], profile["id"]
        fixture = json.loads(raw)
        vectors = fixture.get("fixtures", fixture if isinstance(fixture, list) else [])
        assert len(vectors) == profile["required_vectors"], profile["id"]
        if profile["id"] == "decision-result-hardening-v1":
            assert profile["status"] == "official"
            assert len(vectors) == 31
        if profile["id"] == "runtime-adversarial-v1":
            assert profile["status"] == "official"
            assert len(vectors) == 22
        if profile.get("vector_seals") == "inline vector_hash":
            for vector in vectors:
                assert vector.get("vector_hash") == vector_hash(vector), (profile["id"], vector.get("id"))
        elif profile.get("vector_seals", "").startswith("fixtures/"):
            seal_path = ROOT / profile["vector_seals"].split(";", 1)[0]
            seal_data = json.loads(seal_path.read_text())
            seals = {item.get("name"): item["vector_hash"] for item in seal_data["fixtures"]}
            for vector in vectors:
                assert vector_hash(vector) == seals.get(vector.get("name")), (profile["id"], vector.get("name"))


def test_local_decision_adapter_fails_closed_on_hostile_monkeypatched_producer(monkeypatch):
    import decision as decision_module
    monkeypatch.setattr(decision_module, "decide", lambda *args: ["ALLOW", object()])
    verdict, reason = decide_for_daemon(
        tool_name="send", parameters={"to": "recipient"}, budget_limit_usd=10.0,
        budget_remaining_usd=10.0, max_repeat_calls=3, prior_repeat_count=0,
    )
    assert verdict == "DENY"
    assert reason == "malformed_decision:not_a_pair" or reason.startswith("malformed_decision:")


def test_decision_rejects_adversarial_custom_containers_without_invoking_methods():
    result = decide({"budget_usd": 1.0}, {"seen_nonces": []}, {"tool_name": "x", "params": {"x": HostileList([1])}})
    assert result == ("DENY", "policy_violation:malformed_input")


def test_decision_input_limits_bound_node_and_text_bombs():
    cfg = {"budget_usd": 1.0}
    state = {"seen_nonces": []}
    for payload in ({"x": "a" * 300_000}, {"x": [0] * 50_001}):
        assert decide(cfg, state, {"tool_name": "x", "params": payload})[0] == "DENY"


def test_fail_closed_client_never_authorizes_missing_daemon_even_legacy_fallback(monkeypatch):
    client = ShackleClient(socket_path="/tmp/no-such-shackle.sock", fallback_mode=True)
    async def no_daemon():
        return False
    monkeypatch.setattr(client, "check_daemon", no_daemon)
    result = asyncio.run(client.pre_exec("send", {"to": "x"}))
    assert result == {"decision": "DENY", "reason": "fail_closed:daemon_unavailable"}


def test_daemon_unavailable_denial_prevents_wrapped_tool_dispatch(monkeypatch):
    called = []
    client = ShackleClient(fallback_mode=True)
    async def no_daemon():
        return False
    monkeypatch.setattr(client, "check_daemon", no_daemon)
    @shackled(tool_name="send", estimate_cost=lambda: 0.1, client=client)
    async def action():
        called.append(True)
        return "sent"
    with pytest.raises(PermissionError, match="daemon_unavailable"):
        asyncio.run(action())
    assert not called


def test_failing_estimator_does_not_dispatch_wrapped_function():
    called = []
    class AllowClient:
        async def pre_exec(self, **kwargs):
            return {"decision": "ALLOW", "reason": "test"}
        async def post_exec(self, **kwargs):
            return {"status": "ACK"}
    @shackled(tool_name="must_not_run", estimate_cost=BombEstimate(), client=AllowClient())
    async def action():
        called.append(True)
    with pytest.raises(PermissionError):
        asyncio.run(action())
    assert not called


@pytest.mark.parametrize("bad_estimate", [None, "0", -1, float("nan"), float("inf"), True])
def test_malformed_cost_estimate_does_not_dispatch_wrapped_function(bad_estimate):
    called = []
    class AllowClient:
        async def pre_exec(self, **kwargs):
            return {"decision": "ALLOW", "reason": "test"}
        async def post_exec(self, **kwargs):
            return {"status": "ACK"}
    @shackled(tool_name="must_not_run", estimate_cost=lambda: bad_estimate, client=AllowClient())
    async def action():
        called.append(True)
    with pytest.raises(PermissionError):
        asyncio.run(action())
    assert not called


def test_wrapped_function_has_no_public_wrapped_attribute():
    async def action():
        return "ran"
    guarded = shackled(tool_name="x", client=object())(action)
    assert not hasattr(guarded, "__wrapped__")


def test_client_denies_bad_daemon_decisions_and_does_not_call_tool():
    async def one(payload):
        c = ShackleClient(session_id="s")
        stub = StubClient([StubResponse(payload={"status": "healthy"}), StubResponse(payload=payload)])
        async def get_client(): return stub
        c._get_client = get_client
        outcome = await c.pre_exec("t", {})
        assert outcome["decision"] == "DENY"
        await c.close()
    for payload in ({"decision": "MAYBE"}, ["ALLOW"], {}, {"decision": 1}):
        asyncio.run(one(payload))


def test_daemon_auth_requires_long_secret_and_constant_time_path(monkeypatch):
    async def check():
        monkeypatch.delenv("SHACKLE_SERVICE_TOKEN", raising=False)
        with pytest.raises(HTTPException) as no_secret:
            await daemon.require_service_auth(None)
        assert no_secret.value.status_code == 503
        monkeypatch.setenv("SHACKLE_SERVICE_TOKEN", "s" * 32)
        with pytest.raises(HTTPException) as wrong:
            await daemon.require_service_auth("Bearer wrong")
        assert wrong.value.status_code == 401
        await daemon.require_service_auth("Bearer " + "s" * 32)
    asyncio.run(check())


def test_production_auth_unconfigured_is_unavailable_not_public(monkeypatch):
    async def check():
        monkeypatch.delenv("SHACKLE_HITL_TOKEN", raising=False)
        with pytest.raises(HTTPException) as exc:
            await daemon.require_hitl_auth(None)
        assert exc.value.status_code == 503
    monkeypatch.setenv("SHACKLE_ENV", "production")
    try:
        asyncio.run(check())
    finally:
        monkeypatch.delenv("SHACKLE_ENV", raising=False)


def test_hitl_response_requires_distinct_authentication(monkeypatch):
    async def check():
        monkeypatch.setenv("SHACKLE_HITL_TOKEN", "h" * 32)
        with pytest.raises(HTTPException) as exc:
            await daemon.require_hitl_auth("Bearer " + "x" * 32)
        assert exc.value.status_code == 401
    asyncio.run(check())


def test_hitl_response_single_use_waiter_and_response_replay_denial(monkeypatch):
    async def check():
        monkeypatch.setenv("SHACKLE_HITL_TOKEN", "h" * 32)
        monkeypatch.setenv("SHACKLE_SERVICE_TOKEN", "s" * 32)
        class StateStub:
            async def grant_hitl_execution(self, session_id, request_id): return True
        monkeypatch.setattr(daemon, "state_manager", StateStub())
        request_id = "r" * 24
        token = await daemon._create_hitl_request("s1", request_id)
        body = daemon.HITLResponse(hitl_token=token, decision="ALLOW", session_id="s1", request_id=request_id)
        wrong_binding = daemon.HITLResponse(hitl_token=token, decision="DENY", session_id="other", request_id=request_id)
        with pytest.raises(HTTPException) as mismatch:
            await daemon.hitl_response(wrong_binding)
        assert mismatch.value.status_code == 404
        await daemon.hitl_response(body)
        with pytest.raises(HTTPException) as replay:
            await daemon.hitl_response(body)
        assert replay.value.status_code == 404
        result = await daemon.hitl_wait(daemon.HITLWaitRequest(hitl_token=token))
        assert result["decision"] == "ALLOW"
        with pytest.raises(HTTPException) as consumed:
            await daemon.hitl_wait(daemon.HITLWaitRequest(hitl_token=token))
        assert consumed.value.status_code == 404
        assert token not in daemon.hitl_pending
    asyncio.run(check())


def test_hitl_wait_is_single_claim_under_concurrency(monkeypatch):
    async def check():
        monkeypatch.setenv("SHACKLE_SERVICE_TOKEN", "s" * 32)
        class StateStub:
            async def grant_hitl_execution(self, session_id, request_id): return True
        monkeypatch.setattr(daemon, "state_manager", StateStub())
        request_id = "r" * 24
        token = await daemon._create_hitl_request("s2", request_id)
        first = asyncio.create_task(daemon.hitl_wait(daemon.HITLWaitRequest(hitl_token=token)))
        await asyncio.sleep(0)
        with pytest.raises(HTTPException) as second:
            await daemon.hitl_wait(daemon.HITLWaitRequest(hitl_token=token))
        assert second.value.status_code == 404
        await daemon.hitl_response(daemon.HITLResponse(
            hitl_token=token, decision="DENY", session_id="s2", request_id=request_id))
        assert (await first)["decision"] == "DENY"
    asyncio.run(check())


def test_hitl_expiry_removes_token_and_fails_closed():
    async def check():
        token = await daemon._create_hitl_request("s3", "r" * 24)
        created = daemon.hitl_created_at[token]
        await daemon._expire_hitl_requests(created + daemon._HITL_TTL_SECONDS)
        assert token not in daemon.hitl_pending
        with pytest.raises(HTTPException):
            await daemon.hitl_response(daemon.HITLResponse(
                hitl_token=token, decision="ALLOW", session_id="s3", request_id="r" * 24))
    asyncio.run(check())


def test_hitl_queue_is_bounded():
    async def check():
        old_limit = daemon._MAX_PENDING_HITL
        daemon._MAX_PENDING_HITL = 1
        try:
            await daemon._create_hitl_request("s4", "r" * 24)
            with pytest.raises(HTTPException) as exc:
                await daemon._create_hitl_request("s4", "x" * 24)
            assert exc.value.status_code == 503
        finally:
            daemon._MAX_PENDING_HITL = old_limit
    asyncio.run(check())


def test_state_manager_validates_inputs_before_redis_io():
    async def check():
        manager = StateManager("redis://unused")
        result = await manager.evaluate_and_record("ok", "x", {}, 0.0, request_id="r" * 24)
        assert result["decision"] == "DENY"
        assert result["reason"] == "fail_closed:evaluation_error"
        result = await manager.evaluate_and_record("ok", "x", {}, float("nan"), request_id="n" * 24)
        assert result["decision"] == "DENY"
    asyncio.run(check())


def test_state_manager_rejects_malformed_redis_result(monkeypatch):
    class RedisStub:
        async def eval(self, *args): return [0, "10", "10"]
    async def check():
        manager = StateManager("redis://unused")
        manager.redis = RedisStub()
        result = await manager.evaluate_and_record("ok", "tool", {}, 0.1, request_id="x" * 24)
        assert result["decision"] == "DENY"
        assert result["reason"] == "fail_closed:evaluation_error"
    asyncio.run(check())


def test_route_policy_endpoints_are_authenticated():
    protected = {"/pre_exec", "/post_exec", "/hitl_response", "/hitl_wait"}
    routes = {route.path: route for route in daemon.app.routes}
    for path in protected:
        route = routes[path]
        assert route.dependencies, path


def test_socket_deployment_policy_is_private():
    source = (DAEMON_DIR / "daemon.py").read_text()
    assert "uds_perms = 0o666" not in source
    assert "os.chmod(socket_path, 0o666)" not in source
