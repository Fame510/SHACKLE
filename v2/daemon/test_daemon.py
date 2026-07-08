#!/usr/bin/env python3
"""
Test suite for SHACKLE Daemon
"""

import asyncio
import pytest
from client import ShackleClient, shackled


class TestShackleIntegration:
    """Integration tests for SHACKLE daemon"""
    
    @pytest.fixture
    async def client(self):
        """Create test client"""
        client = ShackleClient(session_id="test_session")
        yield client
        await client.close()
    
    @pytest.mark.asyncio
    async def test_daemon_health(self, client):
        """Test daemon health check"""
        is_available = await client.check_daemon()
        print(f"Daemon available: {is_available}")
        assert isinstance(is_available, bool)
        # Whether up or down, a subsequent benign call must still yield a concrete
        # decision (live) or the documented fallback ALLOW — never an error/None.
        probe = await client.pre_exec(tool_name="health_probe", parameters={"x": 1})
        assert probe.get("decision") in ("ALLOW", "DENY", "HITL")
    
    @pytest.mark.asyncio
    async def test_pre_exec_allow(self, client):
        """Test pre_exec returns ALLOW for normal call"""
        result = await client.pre_exec(
            tool_name="test_tool",
            parameters={"arg": "value"},
            estimated_cost=0.001
        )
        
        assert "decision" in result, "pre_exec must return a decision"
        # A benign first-time call must be ALLOWED — both a healthy daemon and
        # fallback mode return ALLOW here. Accepting DENY (as the old test did)
        # made this assertion vacuous.
        assert result["decision"] == "ALLOW", (
            f"benign call should be ALLOW, got {result['decision']} "
            f"(reason={result.get('reason')})"
        )
        assert result.get("reason"), "an ALLOW decision must carry a reason"
        print(f"Pre-exec result: {result}")
    
    @pytest.mark.asyncio
    async def test_repeat_call_detection(self, client):
        """Test repeat call detection triggers HITL"""
        tool_name = "repeat_test"
        params = {"test": "repeat"}

        # Repeat detection is real daemon behavior; fallback mode always ALLOWs,
        # so this assertion is only meaningful against a live daemon. Skip
        # honestly rather than pretend to test it (the old version had NO
        # assertion at all and could never fail).
        if not await client.check_daemon():
            pytest.skip("daemon unavailable; repeat-detection is not exercised in fallback mode")

        decisions = []
        for i in range(5):
            result = await client.pre_exec(
                tool_name=tool_name,
                parameters=params,
                estimated_cost=0.001,
            )
            decisions.append(result["decision"])
            print(f"Call {i+1}: {result['decision']}")

            if result["decision"] in ("HITL", "DENY"):
                break
            # Record as executed so the repeat counter advances.
            await client.post_exec(
                tool_name=tool_name,
                parameters=params,
                result={"success": True},
                actual_cost=0.001,
                execution_time_ms=10.0,
            )

        # Identical calls repeated up to the limit MUST eventually be gated.
        assert decisions[-1] in ("HITL", "DENY"), (
            f"repeated identical calls should be gated (HITL/DENY), got sequence {decisions}"
        )
    
    @pytest.mark.asyncio
    async def test_decorator_basic(self, client):
        """Test @shackled decorator"""
        
        @shackled(tool_name="decorated_tool", estimate_cost=lambda x: 0.01, client=client)
        async def test_tool(value: str):
            await asyncio.sleep(0.01)
            return {"processed": value}
        
        # A benign decorated call must actually run and return its result.
        # Swallowing PermissionError as a pass (the old behavior) let a wrongly
        # denied tool masquerade as a passing test.
        result = await test_tool("test_value")
        print(f"Tool result: {result}")
        assert result["processed"] == "test_value"
    
    @pytest.mark.asyncio
    async def test_fallback_mode(self):
        """Test fallback when daemon unavailable"""
        # Create client with non-existent socket
        client = ShackleClient(
            socket_path="/tmp/nonexistent.sock",
            session_id="test_fallback",
            fallback_mode=True
        )
        
        result = await client.pre_exec(
            tool_name="fallback_test",
            parameters={"arg": "value"}
        )
        
        # Should allow in fallback mode
        assert result["decision"] == "ALLOW"
        assert "fallback" in result["reason"].lower()
        
        await client.close()


async def run_manual_tests():
    """Run manual integration tests"""
    print("=" * 60)
    print("SHACKLE Daemon Manual Test Suite")
    print("=" * 60)
    
    client = ShackleClient(session_id="manual_test")
    
    # Test 1: Health check
    print("\n[1] Testing daemon health...")
    is_available = await client.check_daemon()
    print(f"    Daemon available: {is_available}")
    
    if not is_available:
        print("    ⚠️  Daemon not available - tests will run in fallback mode")
    
    # Test 2: Simple pre_exec
    print("\n[2] Testing pre_exec...")
    result = await client.pre_exec(
        tool_name="test_tool",
        parameters={"command": "echo hello"},
        estimated_cost=0.001
    )
    print(f"    Decision: {result['decision']}")
    print(f"    Reason: {result.get('reason', 'N/A')}")
    
    # Test 3: Post_exec
    if result["decision"] == "ALLOW":
        print("\n[3] Testing post_exec...")
        post_result = await client.post_exec(
            tool_name="test_tool",
            parameters={"command": "echo hello"},
            result={"output": "hello"},
            actual_cost=0.001,
            execution_time_ms=15.5
        )
        print(f"    Status: {post_result['status']}")
    
    # Test 4: Repeat calls
    print("\n[4] Testing repeat call detection...")
    for i in range(5):
        result = await client.pre_exec(
            tool_name="repeat_test",
            parameters={"iteration": "same"},
            estimated_cost=0.001
        )
        print(f"    Call {i+1}: {result['decision']}")
        
        if result["decision"] == "HITL":
            print(f"    ✓ HITL triggered at call {i+1}")
            print(f"    HITL token: {result.get('hitl_token')}")
            break
        elif result["decision"] == "ALLOW":
            # Record execution
            await client.post_exec(
                tool_name="repeat_test",
                parameters={"iteration": "same"},
                result={"success": True},
                actual_cost=0.001,
                execution_time_ms=10.0
            )
    
    # Test 5: Decorator
    print("\n[5] Testing @shackled decorator...")
    
    @shackled(tool_name="decorated_example", estimate_cost=lambda msg: 0.01, client=client)
    async def example_tool(message: str):
        await asyncio.sleep(0.05)
        return {"echo": message.upper()}
    
    try:
        result = await example_tool("hello world")
        print(f"    ✓ Tool executed: {result}")
    except PermissionError as e:
        print(f"    ✗ Tool denied: {e}")
    
    # Test 6: Budget check
    print("\n[6] Testing budget limits...")
    # Try expensive operation
    result = await client.pre_exec(
        tool_name="expensive_tool",
        parameters={"size": "large"},
        estimated_cost=100.0  # Exceeds default $10 limit
    )
    print(f"    Decision for $100 cost: {result['decision']}")
    print(f"    Reason: {result.get('reason', 'N/A')}")
    
    await client.close()
    
    print("\n" + "=" * 60)
    print("Tests complete!")
    print("=" * 60)


if __name__ == "__main__":
    # Run manual tests
    asyncio.run(run_manual_tests())
