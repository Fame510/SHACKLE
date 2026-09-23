#!/usr/bin/env python3
"""
SHACKLE Client - Thin decorator for tool execution with daemon/fallback support
Auto-detects daemon availability and falls back to local execution
"""

import asyncio
import functools
import httpx
import logging
import math
import os
import secrets
import time
from typing import Any, Callable, Dict, Optional

from shackle.conformance import canonicalization_error

logger = logging.getLogger(__name__)


class ShackleClient:
    """Client for SHACKLE daemon with automatic fallback"""
    
    def __init__(
        self,
        socket_path: Optional[str] = None,
        session_id: Optional[str] = None,
        fallback_mode: bool = False
    ):
        self.socket_path = socket_path or os.getenv("SHACKLE_SOCKET", "/tmp/shackle.sock")
        self.session_id = session_id or os.getenv("SHACKLE_SESSION", "default")
        self._request_id = None
        # Kept as a compatibility parameter only. An unavailable governance
        # service is never a reason to execute without its decision.
        self.fallback_mode = False
        self.service_token = os.getenv("SHACKLE_SERVICE_TOKEN", "")
        self._daemon_available: Optional[bool] = None
        self._client: Optional[httpx.AsyncClient] = None
    
    async def _get_client(self) -> Optional[httpx.AsyncClient]:
        """Get or create HTTP client for Unix socket"""
        if self._client is None:
            try:
                # Create client with Unix socket transport
                transport = httpx.AsyncHTTPTransport(uds=self.socket_path)
                headers = {}
                if self.service_token:
                    headers["Authorization"] = f"Bearer {self.service_token}"
                self._client = httpx.AsyncClient(
                    transport=transport,
                    base_url="http://localhost",
                    timeout=30.0,
                    headers=headers,
                )
            except Exception as e:
                logger.warning(f"Failed to create daemon client: {e}")
                return None
        return self._client
    
    async def check_daemon(self) -> bool:
        """Check fresh daemon readiness; stale cached health is not authorization."""
        try:
            client = await self._get_client()
            if client is None:
                return False
            response = await client.get("/health", timeout=2.0)
            if response.status_code != 200:
                return False
            payload = response.json()
            healthy = type(payload) is dict and payload.get("status") == "healthy"
            if healthy:
                logger.info("SHACKLE daemon is ready")
            return healthy
        except Exception as exc:
            logger.debug("SHACKLE daemon not ready: %s", exc)
            return False
    
    async def pre_exec(
        self,
        tool_name: str,
        parameters: Dict[str, Any],
        estimated_cost: float = 0.0,
        context: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Pre-execution check
        Returns: {"decision": "ALLOW|DENY|HITL", "reason": "...", "hitl_token": "..."}
        """
        # The adapter itself is part of the enforcement boundary: malformed
        # request values must never be coerced into an ALLOW by Pydantic or Lua.
        if (type(tool_name) is not str or not tool_name.strip()
                or type(parameters) is not dict
                or canonicalization_error(parameters) is not None
                or (context is not None and canonicalization_error(context) is not None)
                or type(estimated_cost) not in (int, float)
                or not math.isfinite(estimated_cost) or estimated_cost < 0
                or (context is not None and type(context) is not dict)):
            return {"decision": "DENY", "reason": "fail_closed:malformed_request"}

        # Check if daemon is available
        if not await self.check_daemon():
            # A governance check that cannot reach its decision authority is not
            # authorization. Never turn transport failure into ALLOW, even when
            # the legacy fallback_mode flag is supplied by an older caller.
            logger.error("SHACKLE daemon unavailable; denying %s", tool_name)
            return {"decision": "DENY", "reason": "fail_closed:daemon_unavailable"}
        
        try:
            client = await self._get_client()
            request_id = secrets.token_urlsafe(24)
            resp = await client.post("/pre_exec", json={
                "session_id": self.session_id,
                "request_id": request_id,
                "tool_name": tool_name,
                "parameters": parameters,
                "estimated_cost": estimated_cost,
                "context": context
            })
            
            resp.raise_for_status()
            result = resp.json()
            if type(result) is not dict or type(result.get("decision")) is not str:
                return {"decision": "DENY", "reason": "fail_closed:malformed_daemon_response"}
            if result["decision"] not in {"ALLOW", "DENY", "HITL"}:
                return {"decision": "DENY", "reason": "fail_closed:unknown_daemon_decision"}

            self._request_id = request_id
            # HITL is not permission. It becomes ALLOW only after a valid,
            # token-bound human response; otherwise the wrapper must block.
            if result["decision"] == "HITL":
                hitl_token = result.get("hitl_token")
                if type(hitl_token) is not str or not hitl_token:
                    return {"decision": "DENY", "reason": "fail_closed:missing_hitl_token"}
                hitl_resp = await client.post("/hitl_wait", json={"hitl_token": hitl_token})
                hitl_resp.raise_for_status()
                hitl_data = hitl_resp.json()
                if (type(hitl_data) is not dict
                        or type(hitl_data.get("decision")) is not str
                        or hitl_data["decision"] not in {"ALLOW", "DENY"}):
                    return {"decision": "DENY", "reason": "fail_closed:malformed_hitl_response"}
                if (hitl_data.get("request_id") != request_id
                        or hitl_data.get("session_id") != self.session_id):
                    return {"decision": "DENY", "reason": "fail_closed:hitl_binding_mismatch"}
                result = dict(result)
                result["decision"] = hitl_data["decision"]
                # Free-form notes are evidence, never an authorization token.
                result["reason"] = "human_approved" if hitl_data["decision"] == "ALLOW" else "human_denied"
            result = dict(result)
            result["request_id"] = request_id
            return result

        except Exception as e:
            logger.error("Error in pre_exec; failing closed: %s", e, exc_info=True)
            return {"decision": "DENY", "reason": "fail_closed:daemon_error"}
    
    async def post_exec(
        self,
        tool_name: str,
        parameters: Dict[str, Any],
        result: Optional[Any] = None,
        error: Optional[str] = None,
        actual_cost: float = 0.0,
        execution_time_ms: float = 0.0,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Post-execution logging
        Returns: {"status": "ACK|ERROR", "message": "..."}
        """
        if not await self.check_daemon():
            return {"status": "ERROR", "message": "Daemon unavailable"}
        active_request_id = request_id if request_id is not None else self._request_id
        if type(active_request_id) is not str:
            return {"status": "ERROR", "message": "No execution request is active"}
        
        try:
            client = await self._get_client()
            resp = await client.post("/post_exec", json={
                "session_id": self.session_id,
                "request_id": active_request_id,
                "tool_name": tool_name,
                "parameters": parameters,
                "result": result,
                "error": error,
                "actual_cost": actual_cost,
                "execution_time_ms": execution_time_ms
            })
            
            resp.raise_for_status()
            return resp.json()
            
        except Exception as e:
            logger.error(f"Error in post_exec: {e}", exc_info=True)
            return {"status": "ERROR", "message": str(e)}
    
    async def close(self):
        """Close the client"""
        if self._client:
            await self._client.aclose()
            self._client = None


# Decorator for automatic SHACKLE integration
def shackled(
    tool_name: Optional[str] = None,
    estimate_cost: Optional[Callable] = None,
    client: Optional[ShackleClient] = None
):
    """
    Decorator to wrap tool functions with SHACKLE governance
    
    Usage:
        @shackled(tool_name="my_tool", estimate_cost=lambda *args, **kwargs: 0.01)
        async def my_tool(arg1, arg2):
            # tool implementation
            pass
    """
    def decorator(func: Callable) -> Callable:
        _tool_name = tool_name or func.__name__
        _client = client or ShackleClient()
        
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            start_time = time.time()
            
            # Cost-estimation failure is not a zero-cost authorization. Reject
            # before contacting the policy service rather than silently replacing
            # an untrusted/failed estimate with 0.0.
            estimated_cost = 0.0
            if estimate_cost:
                try:
                    estimated_cost = estimate_cost(*args, **kwargs)
                except Exception as e:
                    logger.error("SHACKLE cost estimate failed; refusing execution: %s", e)
                    raise PermissionError("SHACKLE denied execution: cost estimate unavailable") from e
            if (type(estimated_cost) not in (int, float)
                    or not math.isfinite(estimated_cost) or estimated_cost < 0):
                raise PermissionError("SHACKLE denied execution: invalid cost estimate")

            # Pre-execution check
            pre_result = await _client.pre_exec(
                tool_name=_tool_name,
                parameters={"args": args, "kwargs": kwargs},
                estimated_cost=estimated_cost
            )
            
            # Authorization is an exact allow-list. DENY, HITL, absent, or
            # unknown values all stop here. Never treat a malformed response or
            # an unresolved human-review request as permission to execute.
            decision = pre_result.get("decision") if type(pre_result) is dict else None
            if type(decision) is not str or decision != "ALLOW":
                reason = pre_result.get("reason", "missing or invalid decision") if type(pre_result) is dict else "malformed decision response"
                raise PermissionError(f"SHACKLE denied execution: {reason}")

            # Execute the tool only after an explicit ALLOW.
            result = None
            error = None
            try:
                result = await func(*args, **kwargs)
            except Exception as e:
                error = str(e)
                raise
            finally:
                # Post-execution logging
                execution_time_ms = (time.time() - start_time) * 1000
                
                await _client.post_exec(
                    tool_name=_tool_name,
                    parameters={"args": args, "kwargs": kwargs},
                    result=result,
                    error=error,
                    actual_cost=estimated_cost,  # Reconcile estimate after execution
                    execution_time_ms=execution_time_ms,
                    request_id=pre_result.get("request_id") if type(pre_result) is dict else None,
                )
            
            return result
        
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            # For sync functions, run in asyncio
            loop = asyncio.get_event_loop()
            return loop.run_until_complete(async_wrapper(*args, **kwargs))
        
        # functools.wraps adds __wrapped__, a public raw-function escape hatch.
        # Keep harmless metadata while removing that direct bypass. This does not
        # defend against hostile code introspecting arbitrary Python frames in
        # the same interpreter; that is outside the runtime's trust boundary.
        for wrapped in (async_wrapper, sync_wrapper):
            if hasattr(wrapped, "__wrapped__"):
                del wrapped.__wrapped__

        # Return appropriate wrapper based on function type
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper
    
    return decorator


# Example usage and testing
async def example_usage():
    """Example of using SHACKLE client"""
    
    # Initialize client
    client = ShackleClient(session_id="example_session")
    
    # Define a tool with decorator
    @shackled(tool_name="example_tool", estimate_cost=lambda x: 0.01, client=client)
    async def example_tool(value: str):
        print(f"Executing tool with value: {value}")
        await asyncio.sleep(0.1)
        return {"result": f"processed_{value}"}
    
    # Use the tool - SHACKLE will automatically govern it
    try:
        result = await example_tool("test_value")
        print(f"Tool result: {result}")
    except PermissionError as e:
        print(f"Tool execution denied: {e}")
    finally:
        await client.close()


if __name__ == "__main__":
    # Run example
    asyncio.run(example_usage())
