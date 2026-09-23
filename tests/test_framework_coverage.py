"""Live-framework coverage regressions.

Each test here reproduces a failure that was observed against the real
framework, not a mock of it:

  * CrewAI 1.x (native OpenAI provider + crewai.tools.BaseTool): the original
    litellm and langchain BaseTool hooks saw nothing, so Guard was armed and inert.
  * `from litellm import completion` executed before Guard armed bypassed the
    module-attribute rebind entirely.
  * A framework that catches the exception raised out of a hooked call kept
    running after an operator Abort; the real trigger was lost or misattributed.
  * Provider-prefixed / dated model ids priced at the *default* row
    ("openai/gpt-4" billed at $2/1M input instead of $30/1M).

Tests needing an optional framework skip cleanly when it is not installed
(`pip install -e ".[integration]"` to run them).
"""
import builtins
import json
import os
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from shackle import Guard, ShackleCoverageError, ShackleInterrupt
from shackle import core
from shackle.core import ExecutionState, TriggerEngine, _resolve_pricing

MSG = [{"role": "user", "content": "hello " * 200}]

@pytest.fixture(autouse=True)
def operator_aborts(monkeypatch):
    """Headless operator: every HITL prompt answers Abort."""
    monkeypatch.setattr(builtins, "input", lambda *_: "A")

# ------------------------------------------------------------------ pricing
@pytest.mark.parametrize("model,row", [
    ("gpt-4", "gpt-4"),
    ("openai/gpt-4", "gpt-4"),
    ("azure/gpt-4", "gpt-4"),
    ("gpt-4-0613", "gpt-4"),
    ("gpt-4o", "gpt-4o"),
    ("gpt-4o-2024-08-06", "gpt-4o"),
    ("openai/gpt-4o-mini", "gpt-4o-mini"),
    ("gpt-4o-mini-2024-07-18", "gpt-4o-mini"),
    ("gpt-4-turbo-preview", "gpt-4-turbo"),
    ("anthropic/claude-3-5-sonnet-20241022", "claude-3-5-sonnet"),
    ("GPT-4", "gpt-4"),
])
def test_pricing_resolution(model, row):
    assert _resolve_pricing(model) is core.MODEL_PRICING[row]

def test_pricing_prefix_is_delimiter_bounded():
    # "gpt-4" is a string prefix of "gpt-4o..." but they are different models.
    assert _resolve_pricing("gpt-4o-2024-08-06") is not core.MODEL_PRICING["gpt-4"]

def test_unknown_model_uses_default_but_warns_once(caplog):
    core._PRICING_WARNED.discard("totally-new-model")
    with caplog.at_level("WARNING", logger="shackle"):
        assert _resolve_pricing("totally-new-model") is core.MODEL_PRICING["default"]
        _resolve_pricing("totally-new-model")
    assert sum("no pricing row" in r.message for r in caplog.records) == 1

def test_prefixed_gpt4_is_no_longer_undercounted():
    engine, state = TriggerEngine(budget=100), ExecutionState()
    engine.evaluate_llm_call("openai/gpt-4", 1_000_000, 0, state)
    assert state.total_cost == pytest.approx(30.0)

# ------------------------------------------------- latch + boundary re-raise
def test_swallowed_abort_is_reraised_at_guard_boundary():
    litellm = pytest.importorskip("litellm")
    calls = {"n": 0}

    @Guard(budget=0.001, max_repeat_calls=99, timeout_seconds=60)
    def agent_that_swallows_everything():
        for _ in range(40):
            try:
                litellm.completion(model="gpt-4o", messages=MSG, mock_response="ok " * 300)
                calls["n"] += 1
            except Exception:          # what CrewAI's flow listener effectively does
                pass
        return "finished normally"

    with pytest.raises(ShackleInterrupt) as ei:
        agent_that_swallows_everything()
    assert ei.value.trigger_type == "BUDGET_OVERRUN"     # the real trigger, not a proxy
    assert calls["n"] < 10                               # and it stopped calling after Abort

def test_headless_hitl_fails_closed_instead_of_eof(monkeypatch):
    def eof(*_):
        raise EOFError
    monkeypatch.setattr(builtins, "input", eof)
    si = ShackleInterrupt("x", "BUDGET_EXCEEDED", ExecutionState(), {})
    assert core.render_hitl_terminal(si) == "A"

# ------------------------------------------------------ litellm bypass paths
def _budget_run(call, n=40, delay=0.02, budget=0.001):
    done = {"n": 0}

    @Guard(budget=budget, max_repeat_calls=99, timeout_seconds=60)
    def go():
        for _ in range(n):
            call()
            done["n"] += 1
            time.sleep(delay)          # a real LLM call is never sub-millisecond
    with pytest.raises(ShackleInterrupt) as ei:
        go()
    return ei.value, done["n"]

def test_early_imported_completion_is_governed():
    litellm = pytest.importorskip("litellm")
    from litellm import completion as early              # bound BEFORE Guard arms
    si, n = _budget_run(lambda: early(model="gpt-4o", messages=MSG, mock_response="ok " * 300))
    assert si.trigger_type == "BUDGET_OVERRUN" and n < 10

def test_early_imported_completion_positional_model_is_governed():
    pytest.importorskip("litellm")
    from litellm import completion as early
    si, n = _budget_run(lambda: early("gpt-4o", MSG, mock_response="ok " * 300))
    assert si.trigger_type == "BUDGET_OVERRUN" and n < 10

def test_early_imported_acompletion_is_governed():
    pytest.importorskip("litellm")
    import asyncio
    from litellm import acompletion as early_a
    done = {"n": 0}

    @Guard(budget=0.001, max_repeat_calls=99, timeout_seconds=60)
    async def go():
        for _ in range(40):
            await early_a(model="gpt-4o", messages=MSG, mock_response="ok " * 300)
            done["n"] += 1
            await asyncio.sleep(0.02)
    with pytest.raises(ShackleInterrupt) as ei:
        asyncio.run(go())
    assert ei.value.trigger_type == "BUDGET_OVERRUN" and done["n"] < 10

def test_second_guard_in_same_process_still_accounts():
    """litellm copies CustomLogger instances into its own lists and never removes
    them; a per-Guard instance silently shadowed every later Guard."""
    pytest.importorskip("litellm")
    from litellm import completion as early
    for _ in range(3):
        si, n = _budget_run(lambda: early(model="gpt-4o", messages=MSG, mock_response="ok " * 300))
        assert si.trigger_type == "BUDGET_OVERRUN" and n < 10

def test_litellm_patches_are_fully_reverted():
    litellm = pytest.importorskip("litellm")
    import litellm.utils as lu
    before = (litellm.completion, litellm.acompletion, lu.load_credentials_from_list)

    @Guard(budget=5)
    def go():
        assert litellm.completion is not before[0]
    go()
    assert (litellm.completion, litellm.acompletion, lu.load_credentials_from_list) == before
    assert not core._LITELLM_SCOPES

# ------------------------------------------------------------ coverage report
def test_coverage_gap_is_reported_for_unhooked_framework(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "autogen_agentchat", types.ModuleType("autogen_agentchat"))
    g = Guard(budget=5)
    g(lambda: None)()
    assert any(fw == "autogen_agentchat" for fw, _ in g.last_coverage["gaps"])

def test_strict_guard_refuses_to_arm_with_a_coverage_gap(monkeypatch):
    monkeypatch.setitem(sys.modules, "autogen_agentchat", types.ModuleType("autogen_agentchat"))
    ran = {"n": 0}
    with pytest.raises(ShackleCoverageError):
        Guard(budget=5, strict=True)(lambda: ran.__setitem__("n", 1))()
    assert ran["n"] == 0

def test_inert_guard_is_reported(monkeypatch):
    rep = core._coverage_report({"litellm": None, "basetool": None, "crewai": None, "crewai_hooks": None})
    assert ("none", "no framework hooks attached: this Guard is inert") in rep["gaps"]

# ---------------------------------------------------------------- CrewAI live
class _FakeOpenAI(BaseHTTPRequestHandler):
    hits = 0
    vary = False

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).hits += 1
        n = type(self).hits
        msg = {"role": "assistant", "content": None}
        if body.get("tools"):
            q = "latest AI safety research" + (f" #{n}" if type(self).vary else "")
            msg["tool_calls"] = [{"id": f"call_{n}", "type": "function",
                                  "function": {"name": "web_search", "arguments": json.dumps({"query": q})}}]
            finish = "tool_calls"
        else:
            msg["content"], finish = "Final Answer: gave up.", "stop"
        out = {"id": "x", "object": "chat.completion", "created": 0, "model": "gpt-4o",
               "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
               "usage": {"prompt_tokens": 4000, "completion_tokens": 800, "total_tokens": 4800}}
        raw = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

@pytest.fixture
def crew_env(monkeypatch):
    crewai = pytest.importorskip("crewai")
    monkeypatch.setenv("CREWAI_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    srv = HTTPServer(("127.0.0.1", 0), _FakeOpenAI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _FakeOpenAI.hits, _FakeOpenAI.vary = 0, False
    from crewai import Agent, Crew, LLM, Task
    from crewai.tools import tool
    calls = {"n": 0}

    @tool("web_search")
    def web_search(query: str) -> str:
        """Search the web."""
        calls["n"] += 1
        return "Error: 401 Unauthorized"

    def build():
        llm = LLM(model="openai/gpt-4o", base_url=f"http://127.0.0.1:{srv.server_port}/v1", api_key="x")
        a = Agent(role="Researcher", goal="research", backstory="x", tools=[web_search],
                  llm=llm, max_iter=12, verbose=False)
        return Crew(agents=[a], tasks=[Task(description="Research", expected_output="summary", agent=a)],
                    verbose=False)
    yield types.SimpleNamespace(build=build, calls=calls, server=_FakeOpenAI)
    srv.shutdown()

def test_crewai_native_path_loop_of_death_is_stopped(crew_env):
    @Guard(budget=5, max_repeat_calls=3, timeout_seconds=120)
    def run():
        return crew_env.build().kickoff()
    with pytest.raises(ShackleInterrupt) as ei:
        run()
    assert ei.value.trigger_type == "REPETITIVE_TOOL_CALL"
    assert crew_env.calls["n"] <= 2             # was 12 with an armed-but-inert Guard

def test_crewai_native_path_budget_bites_without_repetition(crew_env):
    crew_env.server.vary = True                  # never repeats: only the budget can stop it

    @Guard(budget=0.05, max_repeat_calls=99, timeout_seconds=120)
    def run():
        return crew_env.build().kickoff()
    with pytest.raises(ShackleInterrupt) as ei:
        run()
    assert ei.value.trigger_type == "BUDGET_OVERRUN"
    assert crew_env.calls["n"] <= 3             # was 12: cost only settled on the final turn

def test_crewai_hook_internal_error_fails_closed(crew_env, monkeypatch):
    real = TriggerEngine.evaluate_tool_call

    def boom(self, agent, tool_name, *a, **k):
        if tool_name == "web_search":            # break only the CrewAI tool-hook path
            raise RuntimeError("bug in engine")
        return real(self, agent, tool_name, *a, **k)
    monkeypatch.setattr(TriggerEngine, "evaluate_tool_call", boom)

    @Guard(budget=5, max_repeat_calls=3, timeout_seconds=120)
    def run():
        return crew_env.build().kickoff()
    with pytest.raises(ShackleInterrupt) as ei:
        run()
    assert ei.value.trigger_type == "HOOK_ERROR"
    assert crew_env.calls["n"] == 0             # crewai swallows hook errors: must deny, not allow

def test_crewai_hooks_are_unregistered_after_guard(crew_env):
    import crewai.hooks as ch
    before = (len(ch.get_before_tool_call_hooks()), len(ch.get_before_llm_call_hooks()),
              len(ch.get_after_llm_call_hooks()))

    @Guard(budget=5, max_repeat_calls=3, timeout_seconds=120)
    def run():
        return crew_env.build().kickoff()
    with pytest.raises(ShackleInterrupt):
        run()
    assert (len(ch.get_before_tool_call_hooks()), len(ch.get_before_llm_call_hooks()),
            len(ch.get_after_llm_call_hooks())) == before
