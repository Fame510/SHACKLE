# <img src="logo.png" width="48" height="48" align="left" alt="SHACKLE logo" style="margin-right: 12px;"> ⛓️ SHACKLE

[![License: AGPLv3](https://img.shields.io/badge/License-AGPLv3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

> **The runtime circuit breaker for autonomous AI agents.**
> One decorator sits inside your runtime and stops runaway loops, budget overruns, and error cascades **before the next tool call fires**. It runs today, it's 100% client-side, and its reference implementation provably passes its own published conformance suite (SP/1.0).

**Status: stable.** SP/1.0 is a published standard: 15 hash-verifiable conformance fixtures, independently reproduced, with the reference implementation passing every vector in CI. The core hooks (litellm + BaseTool) are stable across CrewAI, LangChain/LangGraph, and AutoGen.

```bash
pip install pyshackle
```

---

## ⚡ The Problem

AI agents are capable, but their error handling is broken. When an agent hits an unhandled tool error (401 Unauthorized, a changed API payload, a dead endpoint), it rarely self-corrects. It enters a **"Loop of Death"**: retrying the same tool with the same input, burning your context window and running up a large API bill in minutes.

Frameworks like **CrewAI**, **AutoGen**, and **LangGraph** don't ship a native, framework-agnostic spending guardrail or deterministic loop breaker.

## 🛡️ The Solution

SHACKLE is a lightweight, zero-dependency governance layer that hooks into your runtime via dynamic Python shims. It intercepts **LLM calls** and **tool executions** client-side and tracks execution state deterministically. When an agent breaches your limits, SHACKLE trips the breaker, halts execution, and drops you into an interactive terminal console.

- **1-line install**, no refactoring your agent topology
- **Loop of Death prevention**: detects identical sequential tool calls and error cascades
- **Budget enforcement**: real-time token tracking against a local pricing table
- **Execution timeouts**: no more hung threads on dead APIs
- **HITL console**: interactive Resume / Skip / Abort when a breaker trips
- **100% client-side**: no telemetry, no phone-home, no hidden SaaS

---

## 🚀 Quick Start

**1. Install**

```bash
# From PyPI (import name stays `shackle`)
pip install pyshackle

# Or from source
git clone https://github.com/Fame510/SHACKLE.git
cd SHACKLE && pip install -e .
```

**2. Guard your workflow**

```python
from shackle import Guard
from crewai import Crew

my_crew = Crew(agents=[...], tasks=[...])

# One line to add circuit breaking
@Guard(budget=0.25, max_repeat_calls=3, timeout_seconds=180)
def run():
    return my_crew.kickoff()

run()
```

That's it. SHACKLE dynamically hooks the underlying interpreters, so no framework source changes are needed.

---

## ⚙️ The Four Circuit Breakers

| Trigger | Condition | Default | What happens |
|---|---|---|---|
| **REPETITIVE_TOOL_CALL** | Same tool + same input N times, or input contains error signals | 3 attempts | Drops to HITL console |
| **BUDGET_EXCEEDED** | Accumulated token cost exceeds limit (local pricing table) | $0.20 | Hard execution freeze |
| **TIMEOUT_REACHED** | Wall-clock execution exceeds threshold | 180 seconds | Immediate halt |
| **MAX_TOOL_CALLS** | Total tool invocations exceed limit | 50 calls | Hard stop |

SHACKLE **amplifies sensitivity** when tool inputs contain error signals (`401`, `500`, `timeout`, `unauthorized`, etc.), catching the "I'll just try again" loop before the agent burns tokens on a permission error it can't fix.

---

## 🔌 Works With

| Framework | Support | Notes |
|---|---|---|
| **CrewAI** | ✅ Full | litellm hook + BaseTool hook + Agent.execute_task hook* |
| **LangChain / LangGraph** | ✅ Full | litellm (completion/acompletion) + BaseTool (run/arun) hooks, sync + async |
| **AutoGen** | ✅ Full | litellm interception catches all LLM calls |
| **Smolagents** | ✅ Supported | Manager Agent reasoning-loop detection* |

<sub>\* The core litellm and BaseTool hooks are stable. The newer hooks (CrewAI `Agent.execute_task`, Smolagents reasoning-loop detection) are tracked with per-hook maturity notes in [INTEGRATIONS.md](INTEGRATIONS.md).</sub>

**Deployment modes:** v1 runs in-process — ideal for development, CLI agents, and supervised workflows where a human can act on the HITL prompt. For headless production, the [v2 runtime](v2/README.md) moves decisions to a sidecar daemon with distributed budget state, Ed25519-signed audit logs, and remote HITL control. Same SP/1.0 contract in both modes.

---

## 📚 Going Deeper

The front page keeps it short on purpose. Full detail lives in focused docs:

- **[INTEGRATIONS.md](INTEGRATIONS.md)** — LiteLLM guardrails (pure `decide()` and stateful engine) + proxy `config.yaml`, and the AutoGen wrapper. One LiteLLM integration covers most of the framework stack through a single chokepoint.
- **[CONFORMANCE.md](CONFORMANCE.md)** — the SP/1.0 conformance standard: the `ALLOW / DENY / HITL` decision surface, the `Required ⊆ Supported` model, the 15 hash-verifiable fixtures, and **how any runtime gets certified** and listed in the public [Conformance Registry](https://fame510.github.io/SHACKLE/registry.html).
- **[v2/README.md](v2/README.md)** — the optional enterprise runtime: distributed budget state, cryptographically signed (SOC2-ready) audit logs, and remote HITL control for headless agents.

Run the proof yourself: `pytest tests/test_conformance.py` executes every SP/1.0 vector against the reference implementation.

---

## 👤 Author & License

SHACKLE, the `Required ⊆ Supported` conformance model, the `decide()` surface, and the SP/1.0 HITL transition contract are authored by **Dante Bullock ([@Fame510](https://github.com/Fame510))**, sole author, founder of Sovereign Logic, first published **2026-06-17**. Full provenance and attribution terms are in [CONFORMANCE.md](CONFORMANCE.md).

Licensed under **AGPLv3** — free for individuals, hobbyists, and open-source projects. Shipping SHACKLE inside a closed-source or commercial product? A commercial license removes the copyleft obligation and adds SLA support.

📧 **Commercial licensing, production deployment, or conformance guidance:** docspoc101@gmail.com

> LLM orchestration is non-deterministic. SHACKLE is a best-effort circuit breaker and does not guarantee preventing all API spend overruns; you remain responsible for monitoring your own API limits and bills. See [LICENSE](LICENSE) for full terms.
