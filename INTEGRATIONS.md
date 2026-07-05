# SHACKLE Integrations

First-class SHACKLE SP/1.0 governance for the two biggest chokepoints in the agent
stack: **LiteLLM** (the universal LLM adapter under CrewAI, AutoGen, LangGraph, and
thousands of custom agents) and **Microsoft AutoGen**.

All integrations enforce the same SP/1.0 decision surface (`ALLOW` / `DENY` / `HITL`)
and the transition-validity contract `Valid(t) <=> Required(t) subset-of Supported(t)`.

---

## LiteLLM

`shackle/litellm_shackle_guardrail.py` ships two guardrails. Both raise
`ShackleBlocked` when a call must be stopped, expose synchronous `check()` / `record()`
for SDK use, and implement `async_pre_call_hook` / `async_post_call_success_hook` for
the LiteLLM proxy. `litellm` is an optional dependency: the module imports without it.

### Option A - `ShackleGuardrail` (pure conformance `decide()`)

Stateless with respect to the V2 daemon. Verdicts come straight from the SP/1.0
reference `decide()` in `shackle/conformance.py`, so they are conformance-exact.
Best when you want spec-faithful decisions with minimal dependencies.

```python
from shackle.litellm_shackle_guardrail import ShackleGuardrail, ShackleBlocked
import litellm

guard = ShackleGuardrail(budget_usd=0.50, max_repeat_calls=3, hitl_mode="never")

req = {"model": "gpt-4o", "messages": [{"role": "user", "content": "..."}]}
try:
    guard.check(req)                       # raises ShackleBlocked on DENY/HITL
    resp = litellm.completion(**req)
    guard.record(req, resp)                # track spend from token usage
except ShackleBlocked as b:
    print(b.verdict, b.reason)             # e.g. DENY budget_exhausted
```

### Option B - `ShackleEngineGuardrail` (stateful TriggerEngine)

Drives the full `shackle.core.TriggerEngine` + `ExecutionState` for stateful budget,
repeat, timeout, and tool-count enforcement. Best when you want the same runtime
semantics as the SHACKLE core circuit breaker.

```python
from shackle.litellm_shackle_guardrail import ShackleEngineGuardrail, ShackleBlocked

guard = ShackleEngineGuardrail(budget=0.50, max_repeat_calls=3, timeout_seconds=180)
guard.check(req)
# ... make the call ...
guard.record(req, resp)                    # charges budget from usage; may raise
```

### LiteLLM Proxy (`config.yaml`)

Register either guardrail as a custom guardrail. `mode: [pre_call, post_call]` wires
both hooks (pre-call enforcement + post-call cost tracking).

```yaml
guardrails:
  - guardrail_name: "shackle"
    litellm_params:
      guardrail: shackle.litellm_shackle_guardrail.ShackleGuardrail
      mode: [pre_call, post_call]
      # constructor kwargs:
      budget_usd: 1.0
      max_repeat_calls: 5
      hitl_mode: "never"
```

Swap `ShackleGuardrail` for `ShackleEngineGuardrail` (and `budget_usd` -> `budget`) to
use the stateful engine. Because a proxy has no interactive human, a `HITL` verdict
**fails closed** (blocks the request). Interactive terminal HITL is available for
local SDK runs via `shackle.Guard`.

---

## AutoGen

`shackle/autogen_shackle_wrapper.py` governs AutoGen tools via the real
`TriggerEngine`. AutoGen is an optional dependency: `wrap_tool` works without it;
`create_shackle_agent` requires `pip install pyautogen` (or `autogen-agentchat`).

```python
from shackle.autogen_shackle_wrapper import wrap_tool, create_shackle_agent

@wrap_tool(budget=0.50, max_repeat_calls=3, timeout_seconds=180)
def web_search(query: str):
    return real_search(query)             # raises ShackleBlocked if the loop trips

agent = create_shackle_agent(
    name="Researcher",
    system_message="You are a governed researcher.",
    llm_config=your_llm_config,
    budget=0.50, max_repeat_calls=3,
)
# register wrap_tool-guarded tools on `agent`; every tool call is now governed.
```

Canonical tool-input dedup (`_canonicalize_tool_input`) ensures dict key ordering
cannot evade loop detection.

---

## Recommended deployment

1. **LiteLLM first** - highest blast radius; one integration covers the whole adapter layer.
2. **AutoGen wrapper** for native Microsoft/enterprise AutoGen tool semantics.
3. Combine both: LiteLLM for LLM calls + AutoGen wrapper for native tool calls.

## Installation

```bash
pip install -e .            # SHACKLE core
pip install litellm         # optional, for the LiteLLM guardrail
pip install pyautogen       # optional, for create_shackle_agent
```

> All conformance decisions trace to the SP/1.0 reference `decide()` and the
> hash-pinned fixtures in `fixtures/conformance.json`.
