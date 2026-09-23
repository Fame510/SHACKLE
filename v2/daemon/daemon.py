#!/usr/bin/env python3
"""
SHACKLE Sovereign Daemon - FastAPI server with Unix socket + WebSocket support
Handles pre_exec/post_exec protocol messages for tool execution governance
"""

import asyncio
import hmac
import json
import logging
import math
import os
import secrets
import socket
import stat
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Literal, Optional, Set

import uvicorn
from fastapi import Depends, FastAPI, Header, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from shackle.conformance import canonicalization_error

from state import StateManager
from audit import AuditLogger, load_signing_key

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# Protocol message models
class PreExecRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    session_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    request_id: str = Field(min_length=20, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    tool_name: str = Field(min_length=1, max_length=256)
    parameters: Dict
    estimated_cost: float = Field(default=0.0, ge=0.0, le=1_000_000_000)
    context: Optional[Dict] = None

    @field_validator("session_id", "tool_name")
    @classmethod
    def reject_whitespace_only(cls, value):
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("parameters")
    @classmethod
    def validate_parameters(cls, value):
        if type(value) is not dict or canonicalization_error(value) is not None:
            raise ValueError("parameters must be a bounded canonical JSON object")
        return value

    @field_validator("context")
    @classmethod
    def validate_context(cls, value):
        if value is not None and (type(value) is not dict or canonicalization_error(value) is not None):
            raise ValueError("context must be a bounded canonical JSON object or null")
        return value

    @field_validator("estimated_cost")
    @classmethod
    def validate_finite_cost(cls, value):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("estimated_cost must be finite")
        return float(value)


class PostExecRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    session_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    request_id: str = Field(min_length=20, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    tool_name: str = Field(min_length=1, max_length=256)
    parameters: Dict
    result: Optional[Dict] = None
    error: Optional[str] = Field(default=None, max_length=20_000)
    actual_cost: float = Field(default=0.0, ge=0.0, le=1_000_000_000)
    execution_time_ms: float = Field(default=0.0, ge=0.0, le=31_536_000_000)

    @field_validator("parameters", "result")
    @classmethod
    def validate_post_exec_json(cls, value):
        if value is not None and (type(value) is not dict or canonicalization_error(value) is not None):
            raise ValueError("must be a bounded canonical JSON object or null")
        return value

    @field_validator("actual_cost", "execution_time_ms")
    @classmethod
    def validate_finite_measurement(cls, value):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("measurement must be finite")
        return float(value)


class PreExecResponse(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    decision: Literal["ALLOW", "DENY", "HITL"]
    reason: str = Field(min_length=1, max_length=200)
    hitl_token: Optional[str] = None


class PostExecResponse(BaseModel):
    status: str  # ACK, ERROR
    message: Optional[str] = None


class HITLResponse(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    hitl_token: str = Field(min_length=1, max_length=256)
    decision: Literal["ALLOW", "DENY"]
    notes: Optional[str] = Field(default=None, max_length=2_000)
    request_id: str = Field(min_length=20, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    session_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class HITLWaitRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    hitl_token: str = Field(min_length=1, max_length=256)


async def _verify_bearer_token(authorization: Optional[str], env_name: str) -> None:
    expected = os.getenv(env_name, "")
    # No anonymous fallback: the daemon is an authorization authority, and an
    # unconfigured shared secret means the authority cannot authenticate callers.
    if not expected or len(expected) < 32:
        raise HTTPException(status_code=503, detail=f"{env_name} is not configured with a 32-character secret")
    prefix = "Bearer "
    supplied = authorization[len(prefix):] if type(authorization) is str and authorization.startswith(prefix) else ""
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


async def require_service_auth(authorization: Optional[str] = Header(default=None)) -> None:
    await _verify_bearer_token(authorization, "SHACKLE_SERVICE_TOKEN")


async def require_hitl_auth(authorization: Optional[str] = Header(default=None)) -> None:
    await _verify_bearer_token(authorization, "SHACKLE_HITL_TOKEN")


async def _expire_hitl_requests(now: Optional[float] = None) -> None:
    """Bound pending approval state and remove expired one-shot requests."""
    now = asyncio.get_running_loop().time() if now is None else now
    expired = [token for token, created in hitl_created_at.items()
               if now - created >= _HITL_TTL_SECONDS]
    for token in expired:
        future = hitl_pending.pop(token, None)
        hitl_created_at.pop(token, None)
        hitl_wait_claimed.discard(token)
        hitl_bindings.pop(token, None)
        if future is not None and not future.done():
            future.cancel()


async def _create_hitl_request(session_id: str, request_id: str) -> str:
    async with hitl_lock:
        await _expire_hitl_requests()
        if len(hitl_pending) >= _MAX_PENDING_HITL:
            raise HTTPException(status_code=503, detail="HITL queue is full")
        token = secrets.token_urlsafe(32)
        while token in hitl_pending:
            token = secrets.token_urlsafe(32)
        hitl_pending[token] = asyncio.get_running_loop().create_future()
        hitl_created_at[token] = asyncio.get_running_loop().time()
        hitl_bindings[token] = (session_id, request_id)
        return token


async def _discard_hitl_request(token: str) -> None:
    async with hitl_lock:
        future = hitl_pending.pop(token, None)
        hitl_created_at.pop(token, None)
        hitl_wait_claimed.discard(token)
        hitl_bindings.pop(token, None)
        if future is not None and not future.done():
            future.cancel()


# Global state
state_manager: Optional[StateManager] = None
audit_logger: Optional[AuditLogger] = None
hitl_pending: Dict[str, asyncio.Future] = {}
hitl_wait_claimed: Set[str] = set()
hitl_created_at: Dict[str, float] = {}
hitl_bindings: Dict[str, tuple[str, str]] = {}
hitl_lock = asyncio.Lock()
websocket_connections: Set[WebSocket] = set()
_HITL_TTL_SECONDS = 300.0
_MAX_PENDING_HITL = 4096


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle"""
    global state_manager, audit_logger
    
    logger.info("Starting SHACKLE Daemon...")
    
    # Initialize components
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    postgres_url = os.getenv("POSTGRES_URL", "postgresql://shackle:shackle@localhost:5432/shackle")
    
    state_manager = StateManager(redis_url)
    await state_manager.connect()
    
    # Load a persistent signing key (env/file) so the audit trail verifies across
    # restarts. Falls back to an ephemeral key with a loud warning if unset.
    signing_key, key_is_persistent = load_signing_key()
    if not key_is_persistent:
        # Fail CLOSED in production: an ephemeral signing key makes the audit
        # trail unverifiable across restarts, which defeats the tamper-evident
        # ledger guarantee. Refuse to start rather than run un-auditable.
        if os.getenv("SHACKLE_ENV", "").lower() in ("production", "prod"):
            raise RuntimeError(
                "Refusing to start in production without a persistent audit "
                "signing key. Set SHACKLE_SIGNING_KEY or SHACKLE_SIGNING_KEY_FILE "
                "(inject it from your secrets manager)."
            )
        logger.warning(
            "Audit signing key is EPHEMERAL. Set SHACKLE_SIGNING_KEY (ideally from a "
            "secrets manager) so audit records remain verifiable after restarts."
        )
    audit_logger = AuditLogger(postgres_url, signing_key=bytes(signing_key))
    await audit_logger.connect()
    
    logger.info("SHACKLE Daemon ready")
    
    yield
    
    # Cleanup
    logger.info("Shutting down SHACKLE Daemon...")
    if state_manager:
        await state_manager.close()
    if audit_logger:
        await audit_logger.close()


app = FastAPI(
    title="SHACKLE Sovereign Daemon",
    description="Governance daemon for tool execution control",
    version="2.0.0",
    lifespan=lifespan
)


@app.get("/health")
async def health_check():
    """Public liveness signal; authorization decisions require live dependencies."""
    ready = (
        state_manager is not None and await state_manager.is_connected()
        and audit_logger is not None and audit_logger.is_connected()
    )
    if not ready:
        raise HTTPException(status_code=503, detail="SHACKLE dependencies are not ready")
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.post("/pre_exec", response_model=PreExecResponse, dependencies=[Depends(require_service_auth)])
async def pre_exec(req: PreExecRequest):
    """Evaluate and create a request-bound, one-shot execution capability."""
    if state_manager is None or audit_logger is None:
        raise HTTPException(status_code=503, detail="SHACKLE policy dependencies are unavailable")
    try:
        evaluation = await state_manager.evaluate_and_record(
            session_id=req.session_id, tool_name=req.tool_name, parameters=req.parameters,
            estimated_cost=req.estimated_cost, max_repeat=3, request_id=req.request_id,
        )
        decision = evaluation["decision"]
        reason = evaluation["reason"]
        if type(decision) is not str or decision not in {"ALLOW", "DENY", "HITL"}:
            return PreExecResponse(decision="DENY", reason="fail_closed:invalid_policy_result")
        if type(reason) is not str or not reason:
            return PreExecResponse(decision="DENY", reason="fail_closed:invalid_policy_reason")
        if decision == "DENY":
            await audit_logger.log_decision(req.session_id, req.tool_name, "DENY", reason)
            return PreExecResponse(decision="DENY", reason=reason)
        if decision == "HITL":
            token = await _create_hitl_request(req.session_id, req.request_id)
            try:
                await audit_logger.log_decision(req.session_id, req.tool_name, "HITL", reason)
            except Exception:
                await _discard_hitl_request(token)
                raise
            await broadcast_hitl_request({
                "hitl_token": token, "session_id": req.session_id, "request_id": req.request_id,
                "tool_name": req.tool_name, "parameters": req.parameters, "reason": reason,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            return PreExecResponse(decision="HITL", reason=reason, hitl_token=token)
        if decision != "ALLOW" or evaluation.get("request_id") != req.request_id:
            return PreExecResponse(decision="DENY", reason="fail_closed:invalid_policy_result")
        await audit_logger.log_decision(req.session_id, req.tool_name, "ALLOW", reason)
        return PreExecResponse(decision="ALLOW", reason=reason, hitl_token=req.request_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Error in pre_exec; failing closed: %s", exc, exc_info=True)
        return PreExecResponse(decision="DENY", reason="fail_closed:policy_error")


@app.post("/post_exec", response_model=PostExecResponse, dependencies=[Depends(require_service_auth)])
async def post_exec(req: PostExecRequest):
    """Consume the matching pre-exec capability exactly once, then audit."""
    if state_manager is None or audit_logger is None:
        raise HTTPException(status_code=503, detail="SHACKLE policy dependencies are unavailable")
    try:
        accepted = await state_manager.record_post_exec_once(
            req.session_id, req.request_id, req.tool_name, req.parameters, req.actual_cost,
        )
        if not accepted:
            raise HTTPException(status_code=409, detail="No matching unused execution authorization")
        await audit_logger.log_execution(
            session_id=req.session_id, tool_name=req.tool_name, parameters=req.parameters,
            result=req.result, error=req.error, cost=req.actual_cost,
            execution_time_ms=req.execution_time_ms,
        )
        return PostExecResponse(status="ACK", message="Execution logged")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Error in post_exec; failing closed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Execution accounting failed") from exc


@app.post("/hitl_response", dependencies=[Depends(require_hitl_auth)])
async def hitl_response(resp: HITLResponse):
    """Resolve an authenticated, pending approval exactly once."""
    async with hitl_lock:
        await _expire_hitl_requests()
        future = hitl_pending.get(resp.hitl_token)
        binding = hitl_bindings.get(resp.hitl_token)
        if future is None or future.done() or binding != (resp.session_id, resp.request_id):
            raise HTTPException(status_code=404, detail="HITL token not found, expired, mismatched, or consumed")
        # Retain the future until the sole authenticated waiter consumes it.
        future.set_result(resp)
    return {"status": "ACK", "message": "HITL response recorded"}


@app.post("/hitl_wait", dependencies=[Depends(require_service_auth)])
async def hitl_wait(req: HITLWaitRequest):
    """One authenticated runtime waiter may consume each approval response."""
    token = req.hitl_token
    async with hitl_lock:
        await _expire_hitl_requests()
        future = hitl_pending.get(token)
        if future is None or token in hitl_wait_claimed:
            raise HTTPException(status_code=404, detail="HITL token not found, expired, or already consumed")
        hitl_wait_claimed.add(token)

    try:
        response = await asyncio.wait_for(asyncio.shield(future), timeout=_HITL_TTL_SECONDS)
        async with hitl_lock:
            if hitl_pending.get(token) is not future:
                raise HTTPException(status_code=404, detail="HITL response already consumed")
            hitl_pending.pop(token, None)
            hitl_created_at.pop(token, None)
            hitl_wait_claimed.discard(token)
            hitl_bindings.pop(token, None)
        if response.decision == "ALLOW":
            granted = await state_manager.grant_hitl_execution(response.session_id, response.request_id)
            if not granted:
                return {"decision": "DENY", "notes": "fail_closed:hitl_grant_failed",
                        "request_id": response.request_id, "session_id": response.session_id}
        return {"decision": response.decision, "notes": response.notes,
                "request_id": response.request_id, "session_id": response.session_id}
    except asyncio.TimeoutError:
        async with hitl_lock:
            if hitl_pending.get(token) is future:
                hitl_pending.pop(token, None)
                hitl_created_at.pop(token, None)
                hitl_wait_claimed.discard(token)
                hitl_bindings.pop(token, None)
                if not future.done():
                    future.cancel()
        raise HTTPException(status_code=408, detail="HITL request timed out")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for real-time HITL notifications
    """
    await websocket.accept()
    websocket_connections.add(websocket)
    logger.info(f"WebSocket client connected (total: {len(websocket_connections)})")
    
    try:
        while True:
            # Keep connection alive and listen for messages
            data = await websocket.receive_text()
            msg = json.loads(data)
            
            # WebSocket is notification-only. Approval submissions must use the
            # separately authenticated /hitl_response endpoint; never allow an
            # unauthenticated websocket to resolve an approval token.
                
    except WebSocketDisconnect:
        websocket_connections.remove(websocket)
        logger.info(f"WebSocket client disconnected (total: {len(websocket_connections)})")
    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)
        websocket_connections.discard(websocket)


async def broadcast_hitl_request(data: Dict):
    """Broadcast HITL request to all connected WebSocket clients"""
    message = json.dumps({
        "type": "hitl_request",
        "data": data
    })
    
    disconnected = set()
    for ws in websocket_connections:
        try:
            await ws.send_text(message)
        except Exception as e:
            logger.error(f"Error broadcasting to WebSocket: {e}")
            disconnected.add(ws)
    
    # Clean up disconnected clients
    websocket_connections.difference_update(disconnected)


def run_server():
    """Run the daemon on a private AF_UNIX socket (owner and group only)."""
    socket_path = os.getenv("SHACKLE_SOCKET", "/tmp/shackle.sock")
    socket_file = Path(socket_path)
    socket_file.parent.mkdir(parents=True, exist_ok=True)

    # Refuse symlinks and non-socket collisions rather than deleting an
    # attacker-chosen filesystem target. A stale socket is removable only when
    # lstat confirms it is actually a Unix socket.
    try:
        existing = socket_file.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if not stat.S_ISSOCK(existing.st_mode):
            raise RuntimeError("Refusing to replace non-socket SHACKLE path")
        socket_file.unlink()

    config = uvicorn.Config(app, uds=socket_path, log_level="info", access_log=True)
    server = uvicorn.Server(config)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(server.serve())
    finally:
        try:
            current = socket_file.lstat()
        except FileNotFoundError:
            current = None
        if current is not None and stat.S_ISSOCK(current.st_mode):
            socket_file.unlink()
        loop.close()


if __name__ == "__main__":
    run_server()
