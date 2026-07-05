# SHACKLE Conformance Specification

**Author:** Dante Bullock ([@Fame510](https://github.com/Fame510)) — sole author.
**First published:** 2026-06-17.  **This document:** 2026-07-05.
**Canonical source:** https://github.com/Fame510/SHACKLE

---

## 1. Scope
SHACKLE defines a **verifiable** conformance model for runtime mediation of agent tool calls.
It is a published, hash-chained, independently-reproduced specification — not an ad-hoc pattern,
and not a downstream implementation of any other specification.

## 2. Conformance model
```
Valid(τ)  ⇔  Required(τ) ⊆ Supported(τ)
```
A transition τ is valid iff every required capability is supported.

- **Decision surface:** `ALLOW` / `DENY` / `HITL`
- **Conformance result:** `PASS` / `FAIL` / `NON_CONFORMANT` / `UNTESTABLE`
- **Evidence:** carried separately via `evidence_refs` (format-neutral; e.g. Settlement Attestation Receipts)

The three layers are independent: **control decision** / **conformance result** / **evidence receipt**.

## 3. HITL transition contract (five canonical cases)
| Case | Required behavior |
|------|-------------------|
| **approve** | original call stays executable, bound to the original args digest |
| **reject** | original call becomes terminal / non-dispatchable |
| **modify** | original call terminally superseded; only the edited successor is executable |
| **defer / escalate** | original call stays pending; no execution |
| **duplicate resume** (vs terminal rejected/superseded) | no-effect / fail-closed |

Core invariant: **history-visible ≠ runtime-executable.** Each of the five cases is expressed
as a hash-verifiable fixture under `fixtures/conformance.json` — added 2026-07-05 as
`hitl_transition_approve`, `hitl_transition_reject`, `hitl_transition_modify`,
`hitl_transition_defer_escalate`, and `hitl_transition_duplicate_resume`. Every fixture
carries a canonical SHA-256 hash over its `call.params`; the file now holds 14 vectors total
(9 decision-core + 5 transition).

## 4. Claiming conformance
A runtime is **SHACKLE-conformant** iff it passes the published fixture set at
`Fame510/SHACKLE/fixtures`. Conformance is provable by **reproduction, not assertion**.

**Layer scope (honest):** the conformance-verified layer is this specification plus
`fixtures/conformance.json` (14 vectors) and the reference `shackle/conformance.py::decide()`.
The shipped `shackle/core.py` `@Guard` runtime is a *reference integration*: it uses the same
canonical-hashing discipline as this spec, but does not yet call `decide()` directly. A literal
`core.py → decide()` wiring is on the roadmap; until then, "SP/1.0-conformant" refers to the
spec + fixtures + reference implementation.

> Independent verification of record: @nutstrut reproduced all fixture hashes independently
> (2026-07-05) and published a runnable composition against the published set.

Any runtime — ApprovalNode, `human_approval`, PHI-OMEGA-RUNTIME, or otherwise — may claim
conformance by passing these fixtures. Language-neutral restatements are welcome; the
conformance target remains this published, timestamped fixture set.

## 5. Attribution
SHACKLE, the `Required ⊆ Supported` conformance model, the `decide()` surface, and this
HITL transition contract are authored by **Dante Bullock (@Fame510)**. Implementations,
adapters, and neutral restatements are encouraged — **attribution to SHACKLE as the source
specification is required.**

## 6. Priority / provenance
This specification and its fixtures were first published on **2026-06-17** in this repository,
with full commit history. Any competing authorship or priority claim must be supported by a
dated, publicly published artifact predating that date. Absent such an artifact, this
repository is the authoritative, timestamped source.
