# <img src="logo.png" width="48" height="48" align="left" alt="SHACKLE logo" style="margin-right: 12px;"> ⛓️ SHACKLE

[![License: AGPLv3](https://img.shields.io/badge/License-AGPLv3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

> **The runtime governance layer — and the SP/1.0 conformance standard — for autonomous AI agents.**
> SHACKLE mediates every agent tool call in real time: runaway token loops, unhandled tool cascades, and budget overruns are stopped before the *next* call fires. It is live, it runs today, and its reference implementation provably passes its own conformance suite. This is not a proposal or a prototype — it is a working standard you can run, verify, and be certified against. (Cost is read from usage after each call, so SHACKLE halts the loop, not the single call that crossed the line; see the AGPL best-effort disclaimer below.)

---

## 🔒 SP/1.0 — The Conformance Standard

SHACKLE is not only a runtime circuit breaker — it is the **authored, verifiable conformance standard** for runtime mediation of agent tool calls.

- **Decision surface:** `ALLOW` / `DENY` / `HITL`
- **Conformance model:** `Valid(τ) ⇔ Required(τ) ⊆ Supported(τ)`
- **15 hash-verifiable conformance vectors** in [`fixtures/conformance.json`](fixtures/conformance.json) — 10 decision-core + 5 HITL transition cases (approve / reject / modify / defer-escalate / duplicate-resume)
- **Pure reference implementation:** [`shackle/conformance.py`](shackle/conformance.py) — a stdlib-only `decide(config, state, call) -> (verdict, reason)`
- **Executable proof:** `pytest tests/test_conformance.py` runs every vector against the reference
- **Core invariant:** *history-visible ≠ runtime-executable* — a record that an action happened is not proof the transition was supported

A runtime is **SHACKLE-conformant** iff it passes the published fixture set — provable by **reproduction, not assertion**. See **[CONFORMANCE.md](CONFORMANCE.md)** for the full specification and how to claim conformance. The fixture hashes have been independently reproduced by third parties.

> **Which layer is which:** [`shackle/conformance.py`](shackle/conformance.py) + [`fixtures/conformance.json`](fixtures/conformance.json) are the **conformance-verified layer** — the authored spec and its 15 hash-verifiable vectors, with a reference `decide()`. [`shackle/core.py`](shackle/core.py) is the **shipped runtime integration** (the `@Guard` decorator): it maps its live `TriggerEngine`/`ExecutionState` onto `decide()`'s `(config, state, call)` contract and consults the *same* reference `decide()` on every evaluated tool call and every LLM call, recording the verdict on `state.last_decision`. So "SP/1.0-conformant" refers to the spec, the fixtures, and the reference implementation the shipped runtime actually calls — one decision surface, not two implementations that happen to agree.

**Authorship & provenance:** SHACKLE, the `Required ⊆ Supported` conformance model, the `decide()` surface, and the HITL transition contract are authored by **Dante Bullock ([@Fame510](https://github.com/Fame510))**, sole author. First published 2026-06-17.

---


## 📜 What SP/1.0 Actually Is

**SP/1.0 (SHACKLE Protocol 1.0) is a conformance standard for runtime mediation of autonomous AI agent actions.** In plain terms: it defines — precisely, and in a way anyone can independently verify — what it means for a system to correctly decide whether an agent is *allowed* to take its next action.

Every AI agent, at every step, is about to do something: call a tool, spend budget, invoke another agent, execute a transaction. SP/1.0 governs that moment. It specifies three things no other agent framework pins down as a verifiable contract:

1. **The decision surface — `ALLOW` / `DENY` / `HITL`.** Every mediated action resolves to exactly one verdict: allow it, deny it, or halt for a human. There is no fourth state and no ambiguity. Each verdict carries a deterministic, inspectable reason.
2. **The conformance model — `Valid(τ) ⇔ Required(τ) ⊆ Supported(τ)`.** A transition is valid *if and only if* everything it requires is within what the system provably supports. This is the mathematical spine of the standard: capability is a set relationship, not a promise.
3. **The core invariant — *history-visible ≠ runtime-executable*.** The fact that an action is recorded, resumed, or replayed is **not** evidence that it was ever authorized. A rejected or deferred action that comes back around is denied, not waved through. This single rule closes the class of failures where agents "resume" their way past their own guardrails.

SP/1.0 ships as **15 hash-verifiable conformance vectors** ([`fixtures/conformance.json`](fixtures/conformance.json)) — 10 decision-core cases plus 5 human-in-the-loop transition cases (approve / reject / modify / defer-escalate / duplicate-resume) — and a stdlib-only reference implementation ([`shackle/conformance.py`](shackle/conformance.py)). **You do not take the standard on faith. You run it.** `pytest tests/test_conformance.py` executes every vector against the reference, and the reference implementation passes its own suite — verified, not asserted.

---

## 🎓 What It Means to Be SHACKLE Certified

**SHACKLE Certification is proof — not a promise — that a runtime correctly enforces the SP/1.0 mediation contract.** It is measured entirely against the public, hash-verifiable fixtures. The same vectors that certify you are the ones anyone else can re-run to check your claim. There is no private audit, no trust-us seal, no pay-to-pass: **certification is reproduction.**

For a buyer, "SHACKLE Certified" answers the question every serious acquirer, enterprise, and regulator eventually asks about an autonomous system: *"Can you prove it does what it's allowed to do — and nothing else?"* A certified runtime can. In minutes. From a clean clone.

### Certification Levels

| Level | Name | What it guarantees |
|-------|------|--------------------|
| **SP/1.0-Core** | Core Conformance | The runtime resolves every mediated action to the correct `ALLOW` / `DENY` / `HITL` verdict, with the correct deny reason. The decision surface is sound. |
| **SP/1.0-HITL** | Transition-Complete | Core, plus correct handling of every human-in-the-loop transition: approve, reject, modify, defer-escalate, and duplicate-resume. Rejected and deferred actions provably cannot execute by being replayed. |
| **SP/1.0-Sovereign** | Enterprise Runtime | HITL, plus atomic daemon state, tamper-evident ledgering, and audit export. The full accountability layer an enterprise or acquirer can hold to account. |

### How to Get Certified (open to any runtime, today)

1. **Run the suite** against your runtime: `pytest tests/test_conformance.py` and `pytest v2/tests/test_v2_conformance.py`.
2. **Capture the evidence.** SHACKLE emits a per-fixture pass/fail report with the fixture hashes it verified against. That report *is* your conformance artifact — independently reproducible by anyone.
3. **Submit for listing.** Open a [certification request](https://github.com/Fame510/SHACKLE/issues/new?template=certification_request.yml) with your report and a public, reproducible evidence link. An automated check re-runs the public fixtures; a maintainer verifies before listing.
4. **Get listed.** Verified runtimes appear in the public **[SHACKLE Conformance Registry](https://fame510.github.io/SHACKLE/registry.html)** with the level achieved, SP/1.0 version, and date — and may display the SP/1.0 Certified badge.

### An open standard, an open door

SP/1.0 is a **neutral, public standard**. Any agent runtime — including competing frameworks and competing safety products — is invited to test against it and be listed. The fixtures are public. The verdicts are deterministic. The registry is open. We hold our own reference implementation to exactly the bar we ask of everyone else, and we publish our fixtures so the claim is checkable.

That is the entire philosophy: **a standard is only worth what it can withstand.** If a runtime conforms, the registry proves it. If it doesn't, the fixtures show exactly where. Either way, the measure is public and the same for everyone.

> **Certify your agents.** → [Run the fixtures](fixtures/conformance.json) · [Get listed](https://github.com/Fame510/SHACKLE/issues/new?template=certification_request.yml) · [View the registry](https://fame510.github.io/SHACKLE/registry.html)

## Integrations (LiteLLM + AutoGen)

SHACKLE now ships first-class governance for the biggest chokepoints in the agent stack. See **[INTEGRATIONS.md](INTEGRATIONS.md)** for full usage and LiteLLM proxy `config.yaml` examples.

- **LiteLLM guardrail** (`shackle/litellm_shackle_guardrail.py`) — enforce SP/1.0 on any LiteLLM-powered agent (CrewAI, AutoGen, LangGraph, custom):
  - `ShackleGuardrail` — Option A, backed directly by the pure SP/1.0 reference `decide()` (conformance-exact, dependency-light).
  - `ShackleEngineGuardrail` — Option B, driven by the full `TriggerEngine` (stateful budget / repeat / timeout).
  - Both provide sync `check()`/`record()` for the SDK and `async_pre_call_hook`/`async_post_call_success_hook` for the LiteLLM proxy. `litellm` is an optional dependency.
- **AutoGen wrapper** (`shackle/autogen_shackle_wrapper.py`) — `wrap_tool` decorator + `create_shackle_agent` factory that govern AutoGen tool calls through the same engine. AutoGen is optional; `wrap_tool` works without it.

One integration = coverage for the whole supply chain: because LiteLLM sits under most agent frameworks, the guardrail governs them through a single chokepoint. All decisions trace to the hash-pinned fixtures in `fixtures/conformance.json`.

## Provenance

SHACKLE was built by **Dante Bullock**, a 52-year-old self-taught systems architect and
engineer out of Oakland, California. No venture capital. No corporate incubator.
Just raw necessity and a refusal to watch autonomous agents burn money in
silent infinite loops.

Rather than guessing what the agent ecosystem needed, Sovereign Logic used
real-time web scraping and community sentiment mining to audit the issue
trackers of CrewAI, AutoGen, and LangGraph — mapping the exact systemic
failures affecting developers in production, then building the drop-in
circuit breaker to fix them.

This is infrastructure built by a developer, for developers — sovereign,
lean, and zero-bloat.

---

## 🎯 When to Use SHACKLE

**SHACKLE is purpose-built for:**
- **Local development and debugging** — Interactive HITL console gives you real-time control
- **CLI agents and supervised workflows** — Resume/Skip/Abort when loops are detected
- **Cross-framework coverage** — One decorator works across CrewAI, LangGraph, and AutoGen
- **Budget enforcement** — Client-side token tracking prevents runaway costs
- **Iterative testing** — Catch loops early in the development cycle

**For headless production APIs** (serverless functions, FastAPI endpoints, background workers where blocking for human input isn't an option), consider framework-native solutions like [TokenCircuit](https://github.com/) for automated LangGraph overrides.

SHACKLE and production-oriented tools solve complementary problems: use SHACKLE during development and testing, then transition to automated overrides for deployed APIs if needed.

---

## ⚡ The Problem

AI agents are highly capable, but their error-handling is fundamentally broken. When an agent hits an unhandled tool error (401 Unauthorized, changed API payload, dead endpoint), it rarely self-corrects. Instead, it enters a **"Loop of Death"** — retrying the exact same tool with the exact same input, burning your context window and running up massive API bills in minutes.

Frameworks like **CrewAI**, **AutoGen**, and **LangGraph** lack native, framework-agnostic spending guardrails or deterministic loop breakers.

## 🛡️ The Solution

SHACKLE is a lightweight, zero-dependency governance layer that sits inside your runtime via dynamic Python shims. It intercepts **LLM calls** and **tool executions** client-side, monitoring execution state deterministically.

When an agent breaches your boundaries, SHACKLE trips the circuit breaker, halts execution, and drops you into an interactive terminal console.

### Key Features

- **1-Line Install** — no refactoring your agent topology
- **Loop of Death Prevention** — detects identical sequential tool calls and error cascades
- **Budget Enforcement** — real-time token tracking against a client-side pricing table
- **Execution Timeouts** — prevents hung threads on dead APIs
- **HITL Console** — interactive terminal with Resume / Skip / Abort options
- **100% Client-Side** — no telemetry, no phone-home, no hidden SaaS

---

## 🚀 Quick Start

### 1. Install

> **Note:** Shackle is published on PyPI as the `pyshackle` package (the import name stays `shackle`).

```bash
# From source (available now)
git clone https://github.com/Fame510/SHACKLE.git
cd SHACKLE
pip install -e .

# From PyPI:
pip install pyshackle
```

### 2. Guard Your Workflow

```python
from shackle import Guard
from crewai import Crew, Agent, Task

# Your normal CrewAI setup
my_crew = Crew(agents=[...], tasks=[...])

# One line to add circuit breaking
@Guard(budget=0.25, max_repeat_calls=3, timeout_seconds=180)
def run():
    return my_crew.kickoff()

run()
```

That's it. SHACKLE dynamically hooks the underlying interpreters — no CrewAI source changes needed.

---

## ⚙️ The Four Circuit Breakers

| Trigger | Condition | Default | What Happens |
|---|---|---|---|
| **REPETITIVE_TOOL_CALL** | Same tool + same input called N times, or input contains error signals | 3 attempts | Drops to HITL console |
| **BUDGET_EXCEEDED** | Accumulated token cost exceeds limit (via local pricing table) | $0.20 | Hard execution freeze |
| **TIMEOUT_REACHED** | Wall-clock execution exceeds threshold | 180 seconds | Immediate halt |
| **MAX_TOOL_CALLS** | Total tool invocations exceed limit | 50 calls | Hard stop |

### Error Loop Amplification

SHACKLE **amplifies sensitivity** when tool inputs contain error signals (`401`, `500`, `timeout`, `unauthorized`, etc.) — catching the "I'll just try again" loop before the agent burns tokens on a permission error it can't fix.

---

## 🛠️ The HITL Console

When a breaker trips, SHACKLE renders an interactive terminal:

```
⛓️ SHACKLE CIRCUIT BREAKER: REPETITIVE_TOOL_CALL

Agent:         ResearchAgent
Tool:          web_search
Input:         {"query": "latest AI news", "error": "401 Unauthorized"}
Call Count:    3x
━━━ Session Stats ━━━
Tokens:        In: 8,400 | Out: 1,200
Session Cost:  $0.02850
Time Running:  47.2s

Options:
  [R] Resume/Reset — clear history, continue execution
  [S] Skip — return dummy output, attempt context flush
  [A] Abort — hard terminate the current run

Select action (R/S/A):
```

---

## 🔌 Works With

| Framework | Support | Notes |
|---|---|---|
| **CrewAI** | ✅ Full | litellm hook + BaseTool hook + Agent.execute_task (experimental) |
| **LangChain / LangGraph** | Sync + async | litellm (completion/acompletion) + BaseTool (run/arun) hooks cover sync and async paths |
| **AutoGen** | ✅ Full | litellm interception catches all LLM calls |
| **Smolagents** | 🧪 Experimental | Manager Agent reasoning loop detection active |

---

## 🚀 V2: Enterprise Runtime Sovereignty Layer (Optional)

For production deployments requiring **distributed state**, **compliance audit logs**, or **remote agent control**, see **[v2/README.md](v2/README.md)**.

**V2 adds:**
- ✅ Distributed budget tracking (across serverless functions, Lambda, K8s)
- ✅ Postgres audit logs (cryptographically signed, SOC2-ready)
- ✅ Remote HITL control (manage headless agents from mobile/web)
- ✅ Commercial licensing (for closed-source products)

**V1 (this)** is always free and perfect for local development. **V2** is an optional upgrade for enterprise production use.

---

## 🔮 Roadmap

- [x] Budget enforcement (client-side pricing table)
- [x] Loop of Death detection (repeat tool calls + error amplification)
- [x] HITL terminal interface (Resume / Skip / Abort)
- [x] Execution timeout guard
- [x] **V2: Distributed state engine** (Redis + Postgres)
- [x] **V2: SOC2 compliance pack** (cryptographic audit logs)
- [ ] `.shackle.yaml` config file support
- [ ] Webhook mode for async HITL (instead of CLI)
- [ ] Multi-agent cost attribution dashboard (Pro)
- [ ] Slack / PagerDuty alerts (Pro)

---

## 💰 Commercial Licensing

SHACKLE is open-source under **AGPLv3** — free for individual developers,
hobbyists, and open-source projects. If you're using SHACKLE in a closed-source
commercial product, SaaS platform, or enterprise deployment, the AGPLv3
requires you to open-source your entire application. Most companies don't
want to do that — so they purchase a commercial license instead.

### What a Commercial License Gets You

| | AGPLv3 (Free) | Commercial License |
|---|---|---|
| Use in closed-source products | ❌ | ✅ |
| White-label / rebrand | ❌ | ✅ |
| No copyleft obligations | ❌ | ✅ |
| Priority support | Community | SLA-backed |
| Custom integration assistance | Self-serve | Architecture audit |

### Licensing Options

Commercial licensing is available for:
- **Developer / Startup teams** shipping closed-source agent products
- **Enterprise deployments** requiring on-prem, SOC2 compliance, or SLA support
- **Framework companies** (CrewAI, LangGraph, etc.) wanting white-label integration

Pricing is customized based on your needs, team size, and deployment scale.

📧 **Contact for pricing:** docspoc101@gmail.com

---

## ⚠️ Disclaimer of Liability

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

BY USING THIS SOFTWARE, YOU ACKNOWLEDGE THAT LLM ORCHESTRATION IS INHERENTLY
NON-DETERMINISTIC. SHACKLE IS A BEST-EFFORT CIRCUIT BREAKER AND DOES NOT
GUARANTEE PREVENTING ALL API SPEND OVERRUNS. YOU REMAIN SOLELY RESPONSIBLE FOR
MONITORING YOUR OWN API LIMITS AND USAGE BILLS.

## 📄 License

Copyright (C) 2026 Dante Bullock, Sovereign Logic.

Licensed under the GNU Affero General Public License v3.0 (AGPLv3).
See [LICENSE](LICENSE) for full terms.

**Using SHACKLE in a closed-source product?**
[Contact us](mailto:docspoc101@gmail.com) for commercial licensing.

---

## 👤 Creator

**Dante Bullock** — 52-year-old self-taught systems architect from Oakland, California.
Founder of Sovereign Logic. Built SHACKLE out of raw necessity after watching
autonomous agents burn thousands in silent API loops with no native circuit
breaker in sight.

> *"I don't wait for VC validation. I scrape issue trackers, find the bleeding,
> and build the tourniquet."*

GitHub: [@Fame510](https://github.com/Fame510)
Contact: docspoc101@gmail.com

---

## 🤝 Contributing

### Pricing Table Updates

As model providers update pricing, submit PRs to `shackle/core.py` → `MODEL_PRICING`. Contributors who submit verified pricing updates get credited in release notes.

### Adding Framework Hooks

SHACKLE's architecture supports pluggable runtime hooks. To add support for a new framework:

1. Add a `_patch_<framework>()` function following the pattern in `core.py`
2. Register it in `_apply_patches()`
3. Submit a PR with integration tests

---

## 💼 Commercial Support (optional)

SHACKLE is free and open source (AGPLv3). If you want hands-on help deploying it
in your stack, paid implementation and architecture-audit support is available.

**I fix this. Today.**

If your CrewAI / LangGraph / AutoGen agents are burning money in loops and you
need a solution deployed by someone who understands the internals — not a generic
consultant who'll Google "what is CrewAI" on your dime:

📧 **docspoc101@gmail.com**

### Production & Implementation Inquiries

Deploying SHACKLE in production, or need your runtime certified against the SP/1.0
fixtures? This is a conversation, not a checkout.

📧 **docspoc101@gmail.com** — architecture audits, custom configuration, and
conformance guidance for teams shipping agent products.

You'll speak directly to the engineer who authored the standard.

