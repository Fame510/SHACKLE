# <img src="logo.png" width="48" height="48" align="left" alt="SHACKLE logo" style="margin-right: 12px;"> ⛓️ SHACKLE

[![License: AGPLv3](https://img.shields.io/badge/License-AGPLv3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Spec License: CC BY 4.0](https://img.shields.io/badge/Spec%20License-CC%20BY%204.0-lightgrey.svg)](LICENSE-SPEC.md)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

> **Runtime governance and conformance you can reproduce.** SHACKLE adds a Python runtime guard for repeated tool calls, estimated LLM spend, timeouts, and policy decisions. It also publishes **SP/1.0.1**, a versioned mediation contract with public, hash-pinned test profiles.

**Current status (2026-09-23).** The reference code now includes model-aware pricing resolution, latched fail-closed aborts, pre-call timeout/budget gates, two-layer LiteLLM interception (including early-bound imports), native CrewAI hooks, and optional strict coverage-gap detection. These are shipped, regression-tested changes—not a claim that every framework path or deployment is covered. See [INTEGRATIONS.md](INTEGRATIONS.md) for path and version limits.

### What shipped in the latest runtime hardening

- **Model-aware pricing:** exact model IDs, provider prefixes, and delimiter-bounded dated variants resolve to known pricing rows; unknown models use the default row with a warning. This fixes a tested undercount for IDs such as `openai/gpt-4` that previously landed on the generic default.
- **Terminal-abort latching:** if a supported framework catches an interrupt, the original trigger remains latched and is surfaced at the `Guard` boundary; later hooked calls are denied.
- **Pre-call gates:** supported LLM paths check known elapsed-time and exhausted-budget conditions before sending another request. Usage/cost accounting still depends on each integration's available token data and can be eventual on callback paths.
- **Broader tested hooks:** LiteLLM module rebinding plus a call-time gate for early-bound imports, and native CrewAI before-tool / before-LLM / after-LLM hooks.
- **Coverage visibility:** detected hook gaps are reported; `Guard(strict=True)` can refuse to run when detected gaps remain. Detection cannot prove that every custom/dynamic path has been found.

These controls improve tested integration paths; SHACKLE is not a universal process sandbox and does not guarantee exact provider invoices or prevent every possible spend overrun. Keep provider-side limits and monitoring enabled.

> **Certification is bound to an exact artifact.** The public registry and [verification report](CERTIFICATION-VERIFICATION-2026-09-23.md) bind the owner-verified reference v2 runtime to commit `d9fb4e3cdebf8a18caa4e14060ac22208a1ea4a6`, its recorded source digest, configuration, date, and four official hash-pinned profiles. Those profiles contain 15 + 14 + 31 + 22 = **82 cases**. Later hardening at `e93ef3c3060ea787e0d978fefcfa08e93eb0f79b` passed its own local tests and GitHub CI; it has **not** been rebound to the earlier certification entry or verified as the certified artifact. A profile pass is evidence for its named tests and artifact—not a guarantee of absolute security, production enforcement, legal sufficiency, or absence of vulnerabilities.

> **Verification of the latest hardening commit:** reported local runs were **322 passed** from `tests/` and **393 passed** from the repository root. GitHub Actions [SHACKLE CI run 35928877645](https://github.com/Fame510/SHACKLE/actions/runs/35928877645) completed with all five jobs passing (Python 3.10–3.12, v2 daemon, secret scan). [Certification Verify run 35928877761](https://github.com/Fame510/SHACKLE/actions/runs/35928877761) passed conformance and registry checks; its separate `verify` job was skipped. The [Pages build/deploy run 35928875242](https://github.com/Fame510/SHACKLE/actions/runs/35928875242) succeeded.

> **Publication record.** First release commit **2026-06-17** (`9fbf7c3a`); V2 foundation **2026-06-18**. SP/1.0 specification published **2026-06-24**; SP/1.0.1 implementation tightening **2026-07-30**. The original 15-vector surface has historical independent reproductions from July 4 and July 29. See **[PROVENANCE.md](PROVENANCE.md)** for dates, commits, attribution, and boundaries.

```bash
pip install pyshackle
```

---

## ⚡ The Problem

AI agents are capable, but their error handling is broken. When an agent hits an unhandled tool error (401 Unauthorized, a changed API payload, a dead endpoint), it rarely self-corrects. It enters a **"Loop of Death"**: retrying the same tool with the same input, burning your context window and running up a large API bill in minutes.

Frameworks like **CrewAI**, **AutoGen**, and **LangGraph** don't ship a native, framework-agnostic spending guardrail or deterministic loop breaker.

## 🛡️ The Solution

SHACKLE is an in-process Python governance layer with a separate v2 sidecar option. `Guard` tracks execution state and attaches hooks to supported integration paths. When an observed tool/LLM call reaches a configured limit or policy gate, SHACKLE can deny, pause for operator input, or abort. Coverage depends on the framework path, installed integration, and configuration; it is not a universal process sandbox.

- **Loop controls**: detect repeated tool calls and tested error-cascade patterns
- **Spend accounting**: resolve exact, provider-prefixed, and dated model IDs against known pricing rows; unknown models fall back to the default row with a warning
- **Pre-call checks**: enforce known exhausted budget and elapsed-time limits at supported LLM call gates; post-call token usage remains integration-dependent
- **Fail-closed handling**: latch terminal aborts across tested framework exception-swallowing paths
- **Integration coverage visibility**: `Guard(strict=True)` can reject startup when detected coverage gaps remain
- **Local by design for v1**: execution hooks run in the user's Python process; v2 provides a separate daemon architecture

These are tested controls, not a guarantee of final provider billing accuracy, total framework coverage, or prevention of every spend overrun. Keep provider-side caps and monitoring enabled.

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

For the supported hooks on the versions and execution paths you use, this can add governance without changing framework internals. Check `Guard.last_coverage` and consult [INTEGRATIONS.md](INTEGRATIONS.md); the decorator alone does not establish that every provider/tool path is covered.

---

## ⚙️ The Four Circuit Breakers

| Trigger | Condition | Default | Effect on a covered path |
|---|---|---|---|
| **REPETITIVE_TOOL_CALL** | Same tool + same input repeated, with greater sensitivity for tested error signals | 3 attempts | Routes to the configured operator / deny behavior |
| **BUDGET_EXCEEDED** | Estimated accumulated model cost reaches the configured limit | $0.20 | Denies or interrupts the next observed call; provider billing may differ |
| **TIMEOUT_REACHED** | Wall-clock execution exceeds the configured threshold | 180 seconds | Stops a subsequent covered operation; does not forcibly terminate arbitrary native code |
| **MAX_TOOL_CALLS** | Total observed tool invocations exceed the configured limit | 50 calls | Denies a subsequent covered tool call |

Model pricing is resolved against the local table, including recognized provider prefixes and dated IDs. Unknown model rates fall back to the configured default row with a warning. Cost tracking is an estimate; retain provider-side caps and monitoring.

---

## 🔌 Works With

| Framework | Current coverage | Boundary |
|---|---|---|
| **CrewAI** | Native before-tool / before-LLM / after-LLM hooks, plus detected tool/agent hooks | Verified against the supported CrewAI 1.x test path; hook API and provider path matter. Not a claim that every CrewAI version or custom provider path is covered. |
| **LangChain / LangGraph** | LiteLLM completion hooks and LangChain `BaseTool.run/arun` hooks where calls use those paths | No framework-wide coverage claim; unhooked call paths must be assessed separately. |
| **AutoGen** | Supported AutoGen wrapper; LiteLLM gate applies to calls that route through LiteLLM | Guard does not ship a native AutoGen hook. Do not infer all AutoGen LLM calls are intercepted. |
| **Smolagents** | No native Guard hook | Coverage is conditional on routing through a supported LiteLLM path; otherwise use another enforcement integration. |

`Guard.last_coverage` reports detected coverage gaps. `Guard(strict=True)` can refuse to start when it detects a gap; it cannot identify every custom or dynamically loaded path. See [INTEGRATIONS.md](INTEGRATIONS.md) for exact setup and limitations.

**Deployment modes:** v1 runs in-process — ideal for development, CLI agents, and supervised workflows where a human can act on the HITL prompt. For headless production, the [v2 runtime](v2/README.md) moves decisions to a sidecar daemon with distributed budget state, Ed25519-signed audit logs, and remote HITL control. Same SP/1.0 contract in both modes.

---

## 📚 Going Deeper

The front page keeps it short on purpose. Full detail lives in focused docs:

- **[INTEGRATIONS.md](INTEGRATIONS.md)** — LiteLLM guardrails, the supported AutoGen wrapper, and integration-specific coverage limits.
- **[CERTIFICATION.md](CERTIFICATION.md)** — certification policy, exact claim scope, exclusions, evidence requirements, and lifecycle.
- **[CERTIFICATION-VERIFICATION-2026-09-23.md](CERTIFICATION-VERIFICATION-2026-09-23.md)** — owner verification record for the exact registry-bound runtime artifact and official profile hashes.
- **[fixtures/certification-profiles.json](fixtures/certification-profiles.json)** — official profile IDs, applicability and SHA-256 pins (four profiles, 82 cases total).
- **[CONFORMANCE.md](CONFORMANCE.md)** — the SP/1.0 decision surface and conformance model.
- **[v2/README.md](v2/README.md)** — sidecar daemon architecture and setup.

The 322/393 suite counts above describe the later runtime-hardening commit; they do not replace or amend the earlier certification report. For the certification claim, use its own pinned implementation, profile manifest and verification report.

---

## 👤 Author & License

SHACKLE, the `Required ⊆ Supported` conformance model, the `decide()` surface, and the SP/1.0 HITL transition contract are authored by **Dante Bullock ([@Fame510](https://github.com/Fame510))**, sole author, founder of Sovereign Logic, first published **2026-06-17**. Full provenance and attribution terms are in [CONFORMANCE.md](CONFORMANCE.md).

| Component | License |
|---|---|
| **SP-1.0 Specification & conformance fixtures** | [CC BY 4.0](./LICENSE-SPEC.md) — free to implement, attribution required |
| **pyshackle runtime & reference implementation** | [AGPL-3.0](./LICENSE) — commercial licensing available |

Implementing the SP/1.0 specification or running the published fixtures against your own runtime requires no license from us beyond attribution. The pyshackle code stays AGPLv3 — free for individuals, hobbyists, and open-source projects; shipping it inside a closed-source or commercial product requires a commercial license, which removes the copyleft obligation and adds SLA support.

📧 **Commercial licensing, production deployment, or conformance guidance:** docspoc101@gmail.com

> LLM orchestration is non-deterministic. SHACKLE is a best-effort circuit breaker and does not guarantee preventing all API spend overruns; you remain responsible for monitoring your own API limits and bills. See [LICENSE](LICENSE) for full terms.
