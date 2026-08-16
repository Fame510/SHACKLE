# Provenance — SHACKLE / SP-1.0

This file exists so that questions of origin and dating can be settled by inspection instead of
discussion. Every entry below is verifiable against this repository's git history, the GitHub API,
or PyPI. No entry depends on anyone's recollection.

**Author:** Dante Bullock — sole author of SHACKLE and of the SP/1.0 specification.
**Canonical repository:** https://github.com/Fame510/SHACKLE

---

## 1. Publication record

| Date (UTC) | Milestone | Verification |
|---|---|---|
| 2026-06-10 23:45:33 | Repository `Fame510/SHACKLE` created | GitHub API `created_at` |
| 2026-06-17 01:42:42 | `SHACKLE v0.1.0 — Initial release`: runtime enforcement shipped in `shackle/core.py` | commit `9fbf7c3a051c018977cb6b43234d61d651717274` |
| 2026-06-24 16:22:01 | **SP/1.0 formal protocol specification published** | commit `docs: publish SP/1.0 formal protocol specification` → [`SP-1.0-SPECIFICATION.md`](SP-1.0-SPECIFICATION.md) |
| 2026-07-03 21:30:45 | SP/1.0 conformance fixtures published (crosswalk vectors for `decide()`) | [`fixtures/`](fixtures) |
| 2026-07-04 22:24:49 | Conformance runner + hash-chained audit ledger | PR #2 |
| 2026-07-05 05:09:29 | Canonical dated conformance spec + authorship statement | [`CONFORMANCE.md`](CONFORMANCE.md) |
| 2026-07-05 05:29:32 | 15-vector conformance suite complete, including 5 HITL transition vectors with canonical SHA-256 hashes | `fixtures/` + `CONFORMANCE.md` §3 |
| 2026-07-20 12:32:38 | `pyshackle 1.0.0` published to PyPI | https://pypi.org/project/pyshackle/ |

## 2. Independent reproduction

The conformance material has been replayed by a third party who is not affiliated with this project,
on two separate occasions, both on the public record in
[crewAIInc/crewAI#6025](https://github.com/crewAIInc/crewAI/issues/6025):

- **2026-07-04** — the published fixture hashes were independently replayed against the declared
  canonicalization rule; all reproduced.
- **2026-07-29** — the material was re-run against current `master` at `62dcbc7f`: all 15 vectors
  reproduce and the v1 hashes hold.

Reproduction records are scoped to what was actually reproduced: the conformance vectors and their
canonical hashes. They are not an endorsement of any broader claim, and are not cited as one here.

## 3. What SP/1.0 asserts as its own contribution

The following are original to SP/1.0 and dated by the table above. Where later work restates them
under different field names, this file is the reference for which came first:

1. **A normative `ALLOW` / `DENY` / `HITL` decision envelope** — a portable decision surface, rather
   than an implementation-local return value.
2. **Fail-closed as a conformance requirement.** A decision error or a decision timeout must resolve
   to `DENY`. This is normative, not a configuration flag.
3. **Hash-chained decision records** that carry the identity and version of the deciding rule, so a
   decision can be audited after the fact rather than merely logged.
4. **A language-neutral conformance suite** — 15 fixtures with canonical SHA-256 hashes — so that
   conformance is a reproducible test result rather than a claim in prose.

## 4. What this file does not claim

Precision matters more than breadth, so the boundaries are stated explicitly.

- SHACKLE **does not** claim authorship of the phrase *"generation ≠ release authority."* That
  framing, and the tri-state decomposition associated with it, were already present in
  crewAIInc/crewAI#6025 in early June 2026, before this project's first participation in that
  thread. It is prior art belonging to that discussion, and it is cited here as such.
- SHACKLE does not claim priority over any independently dated work that predates the entries in
  §1. Anyone holding an earlier dated artifact for the items in §3 is invited to publish it; this
  file will be corrected against evidence.
- Reproduction by a third party attests to the conformance vectors only, as scoped in §2.

## 5. Claiming interoperability

Implementations are welcome, and no permission is required. What is required, if a system is
described as SP/1.0-conformant, is that the claim reduce to a reproducible result:

1. Run the published fixtures in [`fixtures/`](fixtures) against your implementation.
2. Verify your outputs against the canonical hashes in [`CONFORMANCE.md`](CONFORMANCE.md).
3. State which vectors pass and which do not.

A disagreement with the vectors is an interoperability finding and is useful — open an issue. An
implementation that agrees with the vectors is an implementation of a published specification, and
citing SP/1.0 with its date is the whole of what is asked.

Specification text is licensed CC BY 4.0 ([`LICENSE-SPEC.md`](LICENSE-SPEC.md)); the reference
implementation is AGPL-3.0 ([`LICENSE`](LICENSE)).

---

*Last verified 2026-08-16 against the GitHub API and PyPI.*
