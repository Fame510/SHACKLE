# Provenance — SHACKLE / SP-1.0

This file exists so that questions of origin and dating can be settled by inspection instead of
discussion. Every entry below is verifiable against this repository's git history, the GitHub API,
PyPI, or a linked public comment. No entry depends on anyone's recollection.

**Author:** Dante Bullock — sole author of SHACKLE and of the SP/1.0 specification.
**Canonical repository:** https://github.com/Fame510/SHACKLE

---

## 1. Publication record

All times UTC. Each row names the artifact that makes it checkable.

| Date (UTC) | Milestone | Verification |
|---|---|---|
| 2026-06-17 01:42:42 | **`SHACKLE v0.1.0 — Initial release`** — runtime enforcement shipped in `shackle/core.py` | commit `9fbf7c3a051c018977cb6b43234d61d651717274` |
| 2026-06-18 08:53:12 | SHACKLE V2 introduced | commit `0e8d754d` |
| 2026-06-18 10:18:23 | V2 foundation: protocol spec, `decide()` core, property tests, SOC 2 mapping, TS client | commit `69a7b719` |
| 2026-06-24 16:22:01 | **SP/1.0 formal protocol specification published** | commit `b754193d` → [`SP-1.0-SPECIFICATION.md`](SP-1.0-SPECIFICATION.md) |
| 2026-07-03 21:30:45 | SP/1.0 conformance fixtures published (crosswalk vectors for `decide()`) | commit `2343b7ba` → [`fixtures/`](fixtures) |
| 2026-07-04 22:24:49 | Conformance runner + hash-chained audit ledger | commit `c34c2130` (PR #2) |
| 2026-07-05 05:09:29 | Canonical dated conformance spec + authorship statement | commit `45ea4694` → [`CONFORMANCE.md`](CONFORMANCE.md) |
| 2026-07-05 05:29:32 | 5 HITL transition vectors added with canonical SHA-256 hashes — **14-vector** suite, spec matched to fixtures | commit `d649b87e`; fixtures README v1.1 (`31864209`) |
| 2026-07-20 11:04:05 | Concurrent budget accounting hardened, SP/1.0 conformance kept consistent | commit `e0f7810a` (PR #6) |
| 2026-07-20 12:32:38 | `pyshackle 1.0.0` published to PyPI | https://pypi.org/project/pyshackle/ |
| 2026-07-30 02:02:42 | Spec + fixtures relicensed CC BY 4.0; runtime stays AGPL-3.0 | commit `e851a9ad` → [`LICENSE-SPEC.md`](LICENSE-SPEC.md) |
| 2026-07-30 21:09:05 | **SP/1.0.1** — 9 fail-open paths closed, runtime enforces `decide()`, fixtures sealed with `vector_hash` | commit `639d74e8` |

Two notes on the boundaries of this table, because precision is the point of the file:

- The GitHub repository record dates to 2026-06-10, but it held no SHACKLE release. The first
  released SHACKLE code is the 2026-06-17 commit above, and that is the date this project claims.
- Between 2026-07-05 and 2026-07-30, `decide()` wiring in `core.py` was documented as reference
  integration rather than full runtime enforcement (see the layer-scope note added in `93416835`).
  Runtime enforcement of `decide()` landed with SP/1.0.1 on 2026-07-30. The specification and
  fixture dates are not backdated to cover it.

## 2. Independent reproduction

The conformance material has been replayed by a third party unaffiliated with this project, on two
separate occasions, both on the public record in
[crewAIInc/crewAI#6025](https://github.com/crewAIInc/crewAI/issues/6025):

- **2026-07-04** — the published fixture hashes were replayed independently against the declared
  canonicalization rule; all reproduced.
  [comment](https://github.com/crewAIInc/crewAI/issues/6025#issuecomment-4884134956)
- **2026-07-29** — current `master` at `62dcbc7f` was re-run: all 15 vectors reproduce, and the v1
  hash chain detects tampering and reordering under the published tests.
  [comment](https://github.com/crewAIInc/crewAI/issues/6025#issuecomment-5123740195)

The reproducing party stated the scope of both records himself, and that scope is reproduced here
unchanged rather than widened: *July 4, 2026 — independent reproduction of the 9-fixture conformance
set; July 29, 2026 — independent reproduction of the 15-vector surface at commit `62dcbc7f`*
([comment](https://github.com/crewAIInc/crewAI/issues/6025#issuecomment-5125138712)). These records
attest to the conformance vectors and their hashes. They are not an endorsement of any broader
claim and are not cited as one.

## 3. What SP/1.0 asserts as its own contribution

The following are original to SP/1.0 and dated by §1. Where later work restates them under
different field names, this file is the reference for which came first:

1. **A normative `ALLOW` / `DENY` / `HITL` decision envelope** — a portable decision surface rather
   than an implementation-local return value.
2. **Fail-closed as a conformance requirement.** A decision error or a decision timeout must resolve
   to `DENY` — normative, not a configuration flag. Specified in SP/1.0; enforced end-to-end in the
   runtime as of SP/1.0.1 (2026-07-30).
3. **Hash-chained decision records** carrying the identity and version of the deciding rule, so a
   decision can be audited after the fact rather than merely logged.
4. **A language-neutral conformance suite** with canonical SHA-256 hashes, so conformance is a
   reproducible test result rather than a claim in prose.

## 4. What this file does not claim

- SHACKLE **does not** claim authorship of the phrase *"generation ≠ release authority"* or of the
  runtime release-control framing built around it. crewAIInc/crewAI#6025, *"[FEATURE] Runtime
  release-control mediation layer before agent/tool execution,"* was opened **2026-06-03** by
  another participant and already carried that framing in its opening text — two weeks before
  SHACKLE's first release commit. It is prior art belonging to that discussion and is cited as such.
  SP/1.0's contribution is what §3 lists: the normative envelope, fail-closed as a requirement,
  hash-chained decision records, and the dated conformance suite.
- SHACKLE does not claim priority over any independently dated work predating the entries in §1.
  Anyone holding an earlier dated artifact for the items in §3 is invited to publish it; this file
  is corrected against evidence, as it already has been once.
- Third-party reproduction attests to the conformance vectors only, as scoped in §2.

## 5. Claiming interoperability

Implementations are welcome and no permission is required. What is asked, if a system is described
as SP/1.0-conformant, is that the claim reduce to a reproducible result:

1. Run the published fixtures in [`fixtures/`](fixtures) against your implementation.
2. Verify your outputs against the canonical hashes in [`CONFORMANCE.md`](CONFORMANCE.md).
3. State which vectors pass and which do not.

A disagreement with the vectors is an interoperability finding and is useful — open an issue. An
implementation that agrees with them is an implementation of a published specification, and citing
SP/1.0 with its date is the whole of what is asked.

Specification text is licensed CC BY 4.0 ([`LICENSE-SPEC.md`](LICENSE-SPEC.md)); the reference
implementation is AGPL-3.0 ([`LICENSE`](LICENSE)).

---

*Every date and hash above re-verified 2026-08-16 against the GitHub API, the linked thread
comments, and PyPI.*
