#!/usr/bin/env python3
"""
SHACKLE State Manager - Redis integration for budget tracking, repeat calls, session state
"""

import hashlib
import json
import logging
import math
import re
import secrets
from typing import Dict, Optional

from shackle.conformance import canonicalization_error, has_opaque_context

import redis.asyncio as redis

logger = logging.getLogger(__name__)

# Verified SP/1.0 decision surface. The daemon's verdicts are produced by the
# reference decide() (encoded by fixtures/conformance.json), not by ad-hoc Lua.
from decision import decide_for_daemon, build_call


class StateManager:
    """Manages session state, budgets, and call patterns in Redis"""
    
    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self.redis: Optional[redis.Redis] = None
    
    async def connect(self):
        """Connect to Redis"""
        try:
            self.redis = redis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True
            )
            await self.redis.ping()
            logger.info("Connected to Redis")
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            raise
    
    async def close(self):
        """Close Redis connection"""
        if self.redis:
            await self.redis.close()
            logger.info("Closed Redis connection")
    
    async def is_connected(self) -> bool:
        """Check live Redis readiness; an allocated client is not proof of service."""
        if self.redis is None:
            return False
        try:
            return bool(await self.redis.ping())
        except Exception:
            return False
    
    def _budget_key(self, session_id: str) -> str:
        """Redis key for session budget"""
        return f"shackle:budget:{session_id}"
    
    def _call_history_key(self, session_id: str) -> str:
        """Redis key for call history"""
        return f"shackle:calls:{session_id}"
    
    def _call_hash(self, tool_name: str, parameters: Dict) -> str:
        """Verified canonical identity for a call.

        Uses the SAME canonical_hash as the conformance vectors (full SHA-256,
        tight separators) via decision.build_call, so the daemon's repeat/replay
        identity matches the spec. (Previously used a truncated 16-char sha256
        over {tool, params} with loose separators -- a different function.)
        """
        return build_call(tool_name, parameters)["nonce"]

    async def check_budget(self, session_id: str, estimated_cost: float) -> bool:
        """
        Check if session has budget remaining for this cost
        Returns True if budget allows, False otherwise
        """
        try:
            budget_key = self._budget_key(session_id)
            
            # Get current spent amount (default 0)
            spent = await self.redis.get(budget_key)
            spent = float(spent) if spent else 0.0
            
            # Get budget limit (default: $10)
            limit_key = f"{budget_key}:limit"
            limit = await self.redis.get(limit_key)
            limit = float(limit) if limit else 10.0
            
            # Check if adding this cost would exceed budget
            would_exceed = (spent + estimated_cost) > limit
            
            if would_exceed:
                logger.warning(
                    f"Budget check failed: {session_id} | "
                    f"spent={spent} + cost={estimated_cost} > limit={limit}"
                )
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"Error checking budget: {e}", exc_info=True)
            # Fail CLOSED - deny the call if we can't verify budget.
            # A circuit breaker that fails open is not a circuit breaker.
            return False
    
    async def update_budget(self, session_id: str, actual_cost: float):
        """Update session budget with actual cost"""
        try:
            budget_key = self._budget_key(session_id)
            
            # Increment spent amount
            new_spent = await self.redis.incrbyfloat(budget_key, actual_cost)
            
            # Set expiry (24 hours)
            await self.redis.expire(budget_key, 86400)
            
            logger.info(f"Updated budget: {session_id} | spent={new_spent} | +{actual_cost}")
            
        except Exception as e:
            logger.error(f"Error updating budget: {e}", exc_info=True)
    
    async def set_budget_limit(self, session_id: str, limit: float):
        """Set budget limit for a session"""
        try:
            limit_key = f"{self._budget_key(session_id)}:limit"
            await self.redis.set(limit_key, limit)
            await self.redis.expire(limit_key, 86400)
            logger.info(f"Set budget limit: {session_id} | limit={limit}")
        except Exception as e:
            logger.error(f"Error setting budget limit: {e}", exc_info=True)
    
    async def get_budget_status(self, session_id: str) -> Dict:
        """Get current budget status for session"""
        try:
            budget_key = self._budget_key(session_id)
            limit_key = f"{budget_key}:limit"
            
            spent = await self.redis.get(budget_key)
            spent = float(spent) if spent else 0.0
            
            limit = await self.redis.get(limit_key)
            limit = float(limit) if limit else 10.0
            
            return {
                "spent": spent,
                "limit": limit,
                "remaining": limit - spent,
                "percentage": (spent / limit * 100) if limit > 0 else 0
            }
        except Exception as e:
            logger.error(f"Error getting budget status: {e}", exc_info=True)
            return {"error": str(e)}
    
    async def record_call(self, session_id: str, tool_name: str, parameters: Dict):
        """Record a tool call for repeat detection"""
        try:
            if self.redis is None:
                raise RuntimeError("Redis policy store unavailable")
            call_hash = self._call_hash(tool_name, parameters)
            history_key = self._call_history_key(session_id)
            
            # Store call with timestamp
            call_data = {
                "tool": tool_name,
                "hash": call_hash,
                "timestamp": str(int(asyncio.get_event_loop().time()))
            }
            
            # Add to list (keep last 100 calls)
            await self.redis.lpush(history_key, json.dumps(call_data))
            await self.redis.ltrim(history_key, 0, 99)
            
            # Set expiry
            await self.redis.expire(history_key, 3600)  # 1 hour
            
        except Exception as e:
            logger.error(f"Error recording call: {e}", exc_info=True)
    
    async def check_repeat_call(
        self,
        session_id: str,
        tool_name: str,
        parameters: Dict
    ) -> bool:
        """
        Check if this is a repeat call (same tool + params within recent history)
        Returns True if repeat detected
        """
        try:
            if self.redis is None:
                raise RuntimeError("Redis policy store unavailable")
            call_hash = self._call_hash(tool_name, parameters)
            history_key = self._call_history_key(session_id)
            
            # Get recent call history
            history = await self.redis.lrange(history_key, 0, 19)  # Last 20 calls
            
            if not history:
                return False
            
            # Check for matching hash
            for call_json in history:
                call_data = json.loads(call_json)
                if call_data.get("hash") == call_hash:
                    return True
            
            return False
            
        except Exception as e:
            logger.error(f"Error checking repeat call: {e}", exc_info=True)
            return False
    
    async def get_repeat_count(
        self,
        session_id: str,
        tool_name: str,
        parameters: Dict
    ) -> int:
        """Get count of how many times this exact call has been made recently"""
        try:
            if self.redis is None:
                raise RuntimeError("Redis policy store unavailable")
            call_hash = self._call_hash(tool_name, parameters)
            history_key = self._call_history_key(session_id)
            
            # Get recent call history
            history = await self.redis.lrange(history_key, 0, -1)
            
            # Count matching hashes
            count = 0
            for call_json in history:
                call_data = json.loads(call_json)
                if call_data.get("hash") == call_hash:
                    count += 1
            
            return count
            
        except Exception as e:
            logger.error(f"Error getting repeat count: {e}", exc_info=True)
            return 0
    
    # Lua script: atomic budget + repeat-count check + conditional record.
    # KEYS[1] = budget spent key, KEYS[2] = budget limit key, KEYS[3] = call history list
    # ARGV[1] = estimated_cost, ARGV[2] = call_hash, ARGV[3] = tool_name,
    # ARGV[4] = max_repeat (int), ARGV[5] = default_limit, ARGV[6] = now_ts,
    # ARGV[7] = history_ttl_seconds, ARGV[8] = call_record_json
    # Returns: {decision, repeat_count} where decision is ALLOW|DENY|HITL
    _EVAL_LUA = """
    -- KEYS: spent, limit, history, request reservation, execution capability,
    --       estimated cost, aggregate reserved budget.
    -- ARGV: cost, call hash, tool, max repeat, default limit, timestamp, ttl,
    --       call record, fingerprint.
    local reserved = redis.call('SET', KEYS[4], ARGV[9], 'NX', 'EX', tonumber(ARGV[7]))
    if not reserved then return {0, '0', '0', 0, 1} end
    local cost = tonumber(ARGV[1])
    local spent = tonumber(redis.call('GET', KEYS[1]) or '0')
    local limit = tonumber(redis.call('GET', KEYS[2]) or ARGV[5])
    local reserved_budget = tonumber(redis.call('GET', KEYS[7]) or '0')
    local history = redis.call('LRANGE', KEYS[3], 0, -1)
    local prior = 0
    for _, item in ipairs(history) do
        if string.find(item, '"hash":"' .. ARGV[2] .. '"', 1, true)
           or string.find(item, '"hash": "' .. ARGV[2] .. '"', 1, true) then prior = prior + 1 end
    end
    local remaining = limit - spent - reserved_budget
    local max_repeat = tonumber(ARGV[4])
    local repeat_denied = max_repeat > 0 and (prior + 1) >= max_repeat
    local budget_denied = limit > 0 and (remaining <= 0 or remaining - cost < 0)
    if repeat_denied or budget_denied then return {prior, tostring(remaining), tostring(limit), 0, 0} end
    redis.call('INCRBYFLOAT', KEYS[7], cost)
    redis.call('EXPIRE', KEYS[7], tonumber(ARGV[7]))
    redis.call('LPUSH', KEYS[3], ARGV[8])
    redis.call('LTRIM', KEYS[3], 0, 99)
    redis.call('EXPIRE', KEYS[3], tonumber(ARGV[7]))
    redis.call('SET', KEYS[6], tostring(cost), 'EX', tonumber(ARGV[7]))
    return {prior, tostring(remaining), tostring(limit), 1, 0}
    """

    _POST_EXEC_LUA = """
    local authorized = redis.call('GET', KEYS[1])
    local estimate = redis.call('GET', KEYS[3])
    if not authorized or authorized ~= ARGV[1] or not estimate then return 0 end
    local consumed = redis.call('SET', KEYS[2], ARGV[1], 'NX', 'EX', tonumber(ARGV[3]))
    if not consumed then return 0 end
    local actual = tonumber(ARGV[2])
    local estimated = tonumber(estimate)
    redis.call('INCRBYFLOAT', KEYS[4], actual)
    redis.call('INCRBYFLOAT', KEYS[5], -estimated)
    redis.call('EXPIRE', KEYS[4], tonumber(ARGV[3]))
    redis.call('EXPIRE', KEYS[5], tonumber(ARGV[3]))
    redis.call('DEL', KEYS[1], KEYS[3])
    return 1
    """

    _GRANT_HITL_LUA = """
    local fingerprint = redis.call('GET', KEYS[1])
    local estimate = redis.call('GET', KEYS[3])
    if not fingerprint or not estimate then return 0 end
    local spent = tonumber(redis.call('GET', KEYS[4]) or '0')
    local limit = tonumber(redis.call('GET', KEYS[5]) or ARGV[1])
    local reserved = tonumber(redis.call('GET', KEYS[6]) or '0')
    -- evaluate_and_record already reserved this request's estimated cost before
    -- returning HITL. Do not reserve it a second time on human approval.
    if limit > 0 and spent + reserved > limit then return 0 end
    local issued = redis.call('SET', KEYS[2], fingerprint, 'NX', 'EX', tonumber(ARGV[2]))
    if not issued then return 0 end
    return 1
    """

    _ISSUE_CAPABILITY_LUA = """
    local fingerprint = redis.call('GET', KEYS[1])
    local estimate = redis.call('GET', KEYS[3])
    if not fingerprint or not estimate or fingerprint ~= ARGV[1] then return 0 end
    local issued = redis.call('SET', KEYS[2], fingerprint, 'NX', 'EX', tonumber(ARGV[2]))
    if not issued then return 0 end
    return 1
    """

    @staticmethod
    def call_fingerprint(session_id: str, request_id: str, tool_name: str, parameters: Dict) -> str:
        material = json.dumps({"session_id": session_id, "request_id": request_id,
            "tool_name": tool_name, "parameters": parameters}, sort_keys=True,
            separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _request_key(self, session_id: str, request_id: str) -> str:
        return f"shackle:request:{session_id}:{request_id}"

    def _execution_key(self, session_id: str, request_id: str) -> str:
        return f"shackle:execution-auth:{session_id}:{request_id}"

    async def record_post_exec_once(self, session_id: str, request_id: str,
                                    tool_name: str, parameters: Dict, actual_cost: float) -> bool:
        if (type(session_id) is not str
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id)
                or type(request_id) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", request_id)
                or type(tool_name) is not str or not tool_name.strip()
                or type(parameters) is not dict or canonicalization_error(parameters) is not None
                or type(actual_cost) not in (int, float) or not math.isfinite(actual_cost) or actual_cost < 0):
            return False
        fingerprint = self.call_fingerprint(session_id, request_id, tool_name, parameters)
        budget_key = self._budget_key(session_id)
        result = await self.redis.eval(
            self._POST_EXEC_LUA, 5,
            self._execution_key(session_id, request_id),
            f"shackle:execution-consumed:{session_id}:{request_id}",
            f"shackle:estimated-cost:{session_id}:{request_id}", budget_key,
            f"{budget_key}:reserved", fingerprint, str(actual_cost), "3600")
        return result == 1

    async def issue_execution_capability(self, session_id: str, request_id: str,
                                        tool_name: str, parameters: Dict) -> bool:
        if self.redis is None:
            return False
        fingerprint = self.call_fingerprint(session_id, request_id, tool_name, parameters)
        result = await self.redis.eval(self._ISSUE_CAPABILITY_LUA, 3,
            self._request_key(session_id, request_id), self._execution_key(session_id, request_id),
            f"shackle:estimated-cost:{session_id}:{request_id}", fingerprint, "3600")
        return result == 1

    async def grant_hitl_execution(self, session_id: str, request_id: str) -> bool:
        if self.redis is None:
            return False
        budget_key = self._budget_key(session_id)
        result = await self.redis.eval(self._GRANT_HITL_LUA, 6,
            self._request_key(session_id, request_id), self._execution_key(session_id, request_id),
            f"shackle:estimated-cost:{session_id}:{request_id}", budget_key,
            f"{budget_key}:limit", f"{budget_key}:reserved", "10.0", "3600")
        return result == 1

    async def evaluate_and_record(self, session_id: str, tool_name: str, parameters: Dict,
                                  estimated_cost: float, max_repeat: int = 3,
                                  default_limit: float = 10.0, request_id: str = "") -> Dict:
        """Fail-closed atomic policy evaluation; request IDs are one-shot capabilities."""
        if (type(session_id) is not str
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id)
                or type(request_id) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", request_id)
                or type(tool_name) is not str or not tool_name.strip()
                or type(parameters) is not dict or canonicalization_error(parameters) is not None
                or type(estimated_cost) not in (int, float) or not math.isfinite(estimated_cost) or estimated_cost < 0
                or type(max_repeat) is not int or max_repeat < 0
                or type(default_limit) not in (int, float) or not math.isfinite(default_limit) or default_limit < 0):
            return {"decision": "DENY", "reason": "fail_closed:malformed_request", "repeat_count": 0}
        try:
            if self.redis is None:
                raise RuntimeError("Redis policy store unavailable")
            call_hash = self._call_hash(tool_name, parameters)
            fingerprint = self.call_fingerprint(session_id, request_id, tool_name, parameters)
            budget_key = self._budget_key(session_id)
            now_ts = str(int(asyncio.get_running_loop().time()))
            record = json.dumps({"tool": tool_name, "hash": call_hash, "timestamp": now_ts}, separators=(",", ":"))
            raw = await self.redis.eval(
                self._EVAL_LUA, 7,
                budget_key, f"{budget_key}:limit", self._call_history_key(session_id),
                self._request_key(session_id, request_id), self._execution_key(session_id, request_id),
                f"shackle:estimated-cost:{session_id}:{request_id}", f"{budget_key}:reserved",
                str(estimated_cost), call_hash, tool_name, str(max_repeat), str(default_limit), now_ts,
                "3600", record, fingerprint,
            )
            if type(raw) not in (list, tuple) or len(raw) != 5:
                raise ValueError("malformed atomic state response")
            prior, remaining_raw, limit_raw, recorded, replayed = raw
            remaining, limit = float(remaining_raw), float(limit_raw)
            if (type(prior) is not int or prior < 0 or type(recorded) is not int or recorded not in (0, 1)
                    or type(replayed) is not int or replayed not in (0, 1)
                    or not math.isfinite(remaining) or not math.isfinite(limit) or limit < 0):
                raise ValueError("invalid atomic state values")
            if replayed:
                return {"decision": "DENY", "reason": "fail_closed:request_replay", "repeat_count": prior}
            verdict, reason = decide_for_daemon(
                tool_name=tool_name, parameters=parameters, budget_limit_usd=limit,
                budget_remaining_usd=remaining, max_repeat_calls=max_repeat, prior_repeat_count=prior,
                estimated_cost_usd=estimated_cost,
            )
            if bool(recorded) != (verdict in {"ALLOW", "HITL"}):
                logger.error("Atomic state gate diverges from verified policy; denying")
                return {"decision": "DENY", "reason": "fail_closed:state_decision_divergence", "repeat_count": prior}
            if not recorded:
                return {"decision": verdict, "reason": reason, "repeat_count": prior}
            if verdict == "ALLOW" and not await self.issue_execution_capability(
                    session_id, request_id, tool_name, parameters):
                return {"decision": "DENY", "reason": "fail_closed:capability_issue_failed", "repeat_count": prior}
            return {"decision": verdict, "reason": reason, "repeat_count": prior + 1,
                    "request_id": request_id}
        except Exception as exc:
            logger.error("Atomic evaluation failed; denying: %s", exc, exc_info=True)
            return {"decision": "DENY", "reason": "fail_closed:evaluation_error", "repeat_count": 0}

    async def clear_session(self, session_id: str):
        """Clear all state for a session"""
        try:
            keys_to_delete = [
                self._budget_key(session_id),
                f"{self._budget_key(session_id)}:limit",
                self._call_history_key(session_id)
            ]
            
            await self.redis.delete(*keys_to_delete)
            logger.info(f"Cleared session state: {session_id}")
            
        except Exception as e:
            logger.error(f"Error clearing session: {e}", exc_info=True)


# Need asyncio import
import asyncio
