#!/usr/bin/env python3
"""
SHACKLE State Manager - Redis integration for budget tracking, repeat calls, session state
"""

import hashlib
import json
import logging
from typing import Dict, Optional

import redis.asyncio as redis

logger = logging.getLogger(__name__)


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
    
    def is_connected(self) -> bool:
        """Check if connected to Redis"""
        return self.redis is not None
    
    def _budget_key(self, session_id: str) -> str:
        """Redis key for session budget"""
        return f"shackle:budget:{session_id}"
    
    def _call_history_key(self, session_id: str) -> str:
        """Redis key for call history"""
        return f"shackle:calls:{session_id}"
    
    def _call_hash(self, tool_name: str, parameters: Dict) -> str:
        """Generate hash of tool call for repeat detection"""
        # Create deterministic hash of tool + params
        call_repr = json.dumps({
            "tool": tool_name,
            "params": parameters
        }, sort_keys=True)
        return hashlib.sha256(call_repr.encode()).hexdigest()[:16]
    
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
            # Fail open - allow the call if we can't check
            return True
    
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
    local spent = tonumber(redis.call('GET', KEYS[1]) or '0')
    local limit = tonumber(redis.call('GET', KEYS[2]) or ARGV[5])
    local est = tonumber(ARGV[1])
    if (spent + est) > limit then
        return {'DENY', 0}
    end

    local call_hash = ARGV[2]
    local max_repeat = tonumber(ARGV[4])
    local history = redis.call('LRANGE', KEYS[3], 0, -1)
    local count = 0
    for i, item in ipairs(history) do
        -- match on the stored hash field without full JSON decode
        if string.find(item, '"hash":"' .. call_hash .. '"', 1, true) then
            count = count + 1
        end
    end

    -- Repeat detection mirrors the non-atomic path: a prior identical call must
    -- already exist (count > 0) and the total must exceed max_repeat.
    if count > 0 and count > max_repeat then
        return {'HITL', count}
    end

    -- ALLOW: record this call atomically so concurrent requests can't race.
    redis.call('LPUSH', KEYS[3], ARGV[8])
    redis.call('LTRIM', KEYS[3], 0, 99)
    redis.call('EXPIRE', KEYS[3], tonumber(ARGV[7]))
    return {'ALLOW', count}
    """

    async def evaluate_and_record(
        self,
        session_id: str,
        tool_name: str,
        parameters: Dict,
        estimated_cost: float,
        max_repeat: int = 3,
        default_limit: float = 10.0,
    ) -> Dict:
        """
        Atomically evaluate budget + repeat-call policy and, when ALLOWed, record
        the call. This collapses the previous check_budget -> check_repeat_call ->
        get_repeat_count -> record_call sequence (which had a TOCTOU race under
        concurrency) into a single Redis round-trip via a Lua script.

        Returns {"decision": "ALLOW"|"DENY"|"HITL", "repeat_count": int}.
        On error, fails open with ALLOW to preserve prior behavior.
        """
        try:
            call_hash = self._call_hash(tool_name, parameters)
            history_key = self._call_history_key(session_id)
            budget_key = self._budget_key(session_id)
            limit_key = f"{budget_key}:limit"

            now_ts = str(int(asyncio.get_event_loop().time()))
            call_record = json.dumps({
                "tool": tool_name,
                "hash": call_hash,
                "timestamp": now_ts,
            })

            result = await self.redis.eval(
                self._EVAL_LUA,
                3,
                budget_key, limit_key, history_key,
                str(estimated_cost), call_hash, tool_name,
                str(int(max_repeat)), str(default_limit), now_ts,
                "3600", call_record,
            )

            decision = result[0]
            if isinstance(decision, bytes):
                decision = decision.decode()
            repeat_count = int(result[1])
            return {"decision": decision, "repeat_count": repeat_count}

        except Exception as e:
            logger.error(f"Error in evaluate_and_record: {e}", exc_info=True)
            # Fail open, consistent with check_budget's fail-open behavior.
            return {"decision": "ALLOW", "repeat_count": 0}

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
