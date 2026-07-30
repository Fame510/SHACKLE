# SHACKLE Conformance Fixtures (SP/1.0)

Language-neutral test vectors for the SHACKLE decision core, `decide()`.

Each fixture in `conformance.json` is a canonical **preimage** (config + state + call)
paired with the **expected typed output** (`verdict` + `reason`). Any implementation —
SHACKLE itself, a CrewAI/LangGraph/AutoGen adapter, or a downstream receipts layer —
can run these vectors and diff results. Same input, same verdict, every time.

## Canonicalization

Two digests, two jobs.

**`canonical_hash`** — the ARGUMENT digest. Covers `call.params` only.
```
sha256( json.dumps(params, sort_keys=True, separators=(",",":"),
                   ensure_ascii=False, allow_nan=False).encode("utf-8") )
```

**`vector_hash`** *(added SP/1.0.1)* — the VECTOR seal. Covers the whole
fixture minus the `vector_hash` key itself, under the same discipline.
```
sha256( json.dumps({k:v for k,v in vector.items() if k != "vector_hash"},
                   sort_keys=True, separators=(",",":"),
                   ensure_ascii=False, allow_nan=False).encode("utf-8") )
```

`canonical_hash` pins only the input preimage, so `expected_verdict` and
`expected_reason` could be edited with every published hash still verifying.
`vector_hash` closes that. **`canonical_hash` keeps its SP/1.0 meaning and every
published value is byte-identical** — `vector_hash` is a new field, not a
redefinition.

Implementations MUST:
- sort object keys ascending before hashing,
- use compact separators `(",", ":")` and UTF-8,
- reject `NaN` / `Infinity` and non-string keys **structurally** — see below,
- fail **closed** (HITL/DENY) on context they cannot evaluate — never silent ALLOW.

### The `__noncanonical__` marker is not the rule
JSON cannot literally encode `NaN`, `Infinity`, or a non-string object key, so
`malformed_non_canonical_input` declares that class with the reserved marker
`{"__noncanonical__": true}`. **Passing that vector by special-casing the marker
string is not conformance.** The rule is structural: reject non-objects,
non-string keys, non-finite floats, unrepresentable types, and unbounded
nesting. `conformance-1.0.1.json` and `tests/test_sp101_regressions.py` probe
the structural behaviour directly.

### Opaque context is a product, not a pair
The rule fires when a context-bearing key (`ctx`, `context`, `opaque`,
`raw_context`, `blob` — case- and whitespace-insensitive, at any depth) carries
a non-evaluable value (`opaque`, `unknown`, `unevaluable`, `untestable`, or
anything the guard cannot introspect). Matching only `{"ctx": "opaque"}`
under-blocks; matching any `context` key regardless of value over-blocks. The
negative control `evaluable_context_is_not_opaque_guard` catches the latter.

## Files
| File | Contents |
|---|---|
| `conformance.json` | The **15 published SP/1.0 vectors**. Unchanged in count and in every `canonical_hash`, `expected_verdict` and `expected_reason`; SP/1.0.1 only adds the `vector_hash` field. |
| `conformance-1.0.1.json` | SP/1.0.1 **adversarial** vectors: inputs the SP/1.0 reference implementation allowed and this revision denies or escalates. Includes one flagged `negative_control`. |

## Layers exercised
circuit breaker · nonce/replay · canonicalization · budget (exhausted /
overrun) · repeat · HITL (threshold / always) · opaque-context fail-closed ·
the five HITL transition cases.

> Time windows, global call caps and probabilistic jitter appear in
> `v2/spec/decide.py` but are **not** certified by SP/1.0 — no vector exercises
> them and the certified core does not implement them. An implementation that
> omits them is fully conformant.

**HITL transition contract (SP/1.0 §3):** approve · reject · modify · defer/escalate ·
duplicate-resume. Core invariant: *history-visible ≠ runtime-executable.* These five
transition vectors (added v1.1) live alongside the decision-core vectors in `conformance.json`
(15 fixtures total).

## Control vs. evidence
`decide()` returns only the **control** verdict (ALLOW/DENY/HITL) with zero I/O.
The signed, hash-chained **audit ledger** is a separate evidence layer written after
the fact. A receipts format may consume a decision record; it is not the decision.

Reference implementation + spec: https://github.com/Fame510/SHACKLE
