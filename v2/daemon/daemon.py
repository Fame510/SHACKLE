#!/usr/bin/env python3
"""
SHACKLE Sovereign Daemon - FastAPI server with Unix socket + WebSocket support
Handles pre_exec/post_exec protocol messages for tool execution governance
"""

import asyncio
import json
import logging
import os
import signal
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Set

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel, Field

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
    session_id: str
    tool_name: str
    parameters: Dict
    estimated_cost: float = 0.0
    context: Optional[Dict] = None


class PreExecResponse(BaseModel):
    decision: str  # ALLOW, DENY, HITL
    reason: Optional[str] = None
    hitl_token: Optional[str] = None


class PostExecRequest(BaseModel):
    session_id: str
    tool_name: str
    parameters: Dict
    result: Optional[Dict] = None
    error: Optional[str] = None
    actual_cost: float = 0.0
    execution_time_ms: float = 0.0


class PostExecResponse(BaseModel):
    status: str  # ACK, ERROR
    message: Optional[str] = None


class HITLResponse(BaseModel):
    hitl_token: str
    decision: str  # ALLOW, DENY
    notes: Optional[str] = None


# Global state
state_manager: Optional[StateManager] = None
audit_logger: Optional[AuditLogger] = None
hitl_pending: Dict[str, asyncio.Future] = {}
websocket_connections: Set[WebSocket] = set()


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
    """Health check endpoint"""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "components": {
            "state": state_manager is not None and state_manager.is_connected(),
            "audit": audit_logger is not None and audit_logger.is_connected()
        }
    }


@app.post("/pre_exec", response_model=PreExecResponse)
async def pre_exec(req: PreExecRequest):
    """
    Pre-execution check: evaluate if tool call should proceed
    Returns: ALLOW, DENY, or HITL (human-in-the-loop)
    """
    logger.info(f"pre_exec: {req.session_id} | {req.tool_name}")
    
    try:
        # Atomic budget + repeat evaluation. This single Redis round-trip replaces
        # the previous check_budget -> check_repeat_call -> get_repeat_count ->
        # record_call sequence, eliminating the TOCTOU race where two concurrent
        # requests on the same session could both pass before either was recorded.
        evaluation = await state_manager.evaluate_and_record(
            session_id=req.session_id,
            tool_name=req.tool_name,
            parameters=req.parameters,
            estimated_cost=req.estimated_cost,
            max_repeat=3,
        )
        decision = evaluation["decision"]
        repeat_count = evaluation["repeat_count"]
        # Verified SP/1.0 reason code (e.g. budget_exhausted, max_repeat_exceeded,
        # fail_closed:evaluation_error) produced by decide(), not a hardcoded string.
        reason_code = evaluation.get("reason", "unspecified")

        if decision == "DENY":
            await audit_logger.log_decision(
                session_id=req.session_id,
                tool_name=req.tool_name,
                decision="DENY",
                reason=reason_code,
            )
            return PreExecResponse(decision="DENY", reason=reason_code)

        if decision == "HITL":
            hitl_token = f"hitl_{req.session_id}_{datetime.utcnow().timestamp()}"

            await audit_logger.log_decision(
                session_id=req.session_id,
                tool_name=req.tool_name,
                decision="HITL",
                reason=reason_code,
            )

            # Create future for HITL response
            hitl_pending[hitl_token] = asyncio.Future()

            # Notify WebSocket clients
            await broadcast_hitl_request({
                "hitl_token": hitl_token,
                "session_id": req.session_id,
                "tool_name": req.tool_name,
                "parameters": req.parameters,
                "reason": f"Repeat call ({repeat_count} times)",
                "timestamp": datetime.utcnow().isoformat(),
            })

            return PreExecResponse(
                decision="HITL",
                reason=reason_code,
                hitl_token=hitl_token,
            )

        # ALLOW: the call was already recorded atomically inside evaluate_and_record.
        await audit_logger.log_decision(
            session_id=req.session_id,
            tool_name=req.tool_name,
            decision="ALLOW",
            reason=reason_code,
        )

        return PreExecResponse(decision="ALLOW", reason=reason_code)

    except Exception as e:
        logger.error(f"Error in pre_exec: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/post_exec", response_model=PostExecResponse)
async def post_exec(req: PostExecRequest):
    """
    Post-execution logging: update counters and write audit log
    """
    logger.info(f"post_exec: {req.session_id} | {req.tool_name} | {req.actual_cost}")
    
    try:
        # Update budget
        await state_manager.update_budget(
            req.session_id,
            req.actual_cost
        )
        
        # Log execution
        await audit_logger.log_execution(
            session_id=req.session_id,
            tool_name=req.tool_name,
            parameters=req.parameters,
            result=req.result,
            error=req.error,
            cost=req.actual_cost,
            execution_time_ms=req.execution_time_ms
        )
        
        return PostExecResponse(
            status="ACK",
            message="Execution logged"
        )
        
    except Exception as e:
        logger.error(f"Error in post_exec: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/hitl_response")
async def hitl_response(resp: HITLResponse):
    """
    Human-in-the-loop response endpoint
    """
    logger.info(f"hitl_response: {resp.hitl_token} | {resp.decision}")
    
    if resp.hitl_token not in hitl_pending:
        raise HTTPException(status_code=404, detail="HITL token not found or expired")
    
    try:
        # Resolve the pending future
        future = hitl_pending.pop(resp.hitl_token)
        future.set_result(resp)
        
        return {"status": "ACK", "message": "HITL response recorded"}
        
    except Exception as e:
        logger.error(f"Error in hitl_response: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/hitl_wait/{hitl_token}")
async def hitl_wait(hitl_token: str):
    """
    Blocking endpoint to wait for HITL response
    Used by clients that don't have WebSocket support
    """
    if hitl_token not in hitl_pending:
        raise HTTPException(status_code=404, detail="HITL token not found")
    
    try:
        # Wait for human response (with timeout)
        future = hitl_pending[hitl_token]
        resp = await asyncio.wait_for(future, timeout=300.0)  # 5 min timeout
        
        return {
            "decision": resp.decision,
            "notes": resp.notes
        }
        
    except asyncio.TimeoutError:
        hitl_pending.pop(hitl_token, None)
        raise HTTPException(status_code=408, detail="HITL request timed out")
    except Exception as e:
        logger.error(f"Error in hitl_wait: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


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
            
            # Handle HITL responses via WebSocket
            if msg.get("type") == "hitl_response":
                resp = HITLResponse(**msg["data"])
                await hitl_response(resp)
                
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
    """Run the FastAPI server on Unix socket"""
    socket_path = os.getenv("SHACKLE_SOCKET", "/tmp/shackle.sock")
    
    # Remove existing socket
    if os.path.exists(socket_path):
        os.remove(socket_path)
    
    # Run with uvicorn
    config = uvicorn.Config(
        app,
        uds=socket_path,
        log_level="info",
        access_log=True
    )
    server = uvicorn.Server(config)
    
    # Set socket permissions
    def set_permissions():
        os.chmod(socket_path, 0o666)
    
    # Run server
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        loop.run_until_complete(server.serve())
    finally:
        if os.path.exists(socket_path):
            os.remove(socket_path)


if __name__ == "__main__":
    run_server()
