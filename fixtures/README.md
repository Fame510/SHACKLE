# SHACKLE Conformance Fixtures (SP/1.0)

Language-neutral test vectors for the SHACKLE decision core, `decide()`.

Each fixture in `conformance.json` is a canonical **preimage** (config + state + call)
paired with the **expected typed output** (`verdict` + `reason`). Any implementation —
SHACKLE itself, a CrewAI/LangGraph/AutoGen adapter, or a downstream receipts layer —
can run these vectors and diff results. Same input, same verdict, every time.

## Canonicalization
`canonical_hash = sha256( json.dumps(params, sort_keys=True, separators=(",", ":")) )`

Implementations MUST:
- sort object keys ascending before hashing,
- use compact separators `(",", ":")` and UTF-8,
- reject `NaN` / `Infinity` and non-string keys,
- fail **closed** (HITL/DENY) on context they cannot evaluate — never silent ALLOW.

## Layers exercised
circuit breaker · nonce/replay · budget · repeat (incl. error amplification) ·
window · global cap · probabilistic jitter · HITL (threshold / always).

**HITL transition contract (SP/1.0 §3):** approve · reject · modify · defer/escalate ·
duplicate-resume. Core invariant: *history-visible ≠ runtime-executable.* These five
transition vectors (added v1.1) live alongside the decision-core vectors in `conformance.json`
(15 fixtures total).

## Control vs. evidence
`decide()` returns only the **control** verdict (ALLOW/DENY/HITL) with zero I/O.
The signed, hash-chained **audit ledger** is a separate evidence layer written after
the fact. A receipts format may consume a decision record; it is not the decision.

Reference implementation + spec: https://github.com/Fame510/SHACKLE
