# SHACKLE reference runtime verification record

**Verification date:** 2026-09-23  
**Tested implementation commit:** `a45eaa863a7d2afb0847ce81d0952737abdf98fa`
**Source archive SHA-256:** `9aef63709dd0d8241398c0bea095521e5258067d0caca3ca24e1b9dc0fe078cd`
**GitHub Actions on this commit:** [SHACKLE CI 35953824334](https://github.com/Fame510/SHACKLE/actions/runs/35953824334), [Certification Verify 35953824332](https://github.com/Fame510/SHACKLE/actions/runs/35953824332), [Pages deployment 35953823384](https://github.com/Fame510/SHACKLE/actions/runs/35953823384) — all completed successfully
**Verifier:** repository owner, local self-verification

## Current corrected artifact

The registry and this report bind the current owner-verified result to the exact implementation commit and source archive above. Fresh verification was run after that source commit existed:

```text
pytest tests/             323 passed, 0 failed
pytest (repository root)  394 passed, 0 failed
python tools/verify_certification.py
PASS — commit a45eaa863a7d2afb0847ce81d0952737abdf98fa
python tools/certification_pipeline.py validate-registry registry.json
registry OK
```

The independent reviewer found that CrewAI hooks and LiteLLM both booked usage for the same CrewAI LLM when it was explicitly routed through LiteLLM. The live regression reproduced the pre-fix behavior: five fake provider requests generated ten SHACKLE accounting events and booked 40,000 input tokens. On the corrected code it records exactly five events for five requests: 20,000 input tokens and 4,000 output tokens total. The fix skips CrewAI settlement for an LLM marked `is_litellm`; the LiteLLM accounting layer remains the single booking source for that route.

The four official profile IDs, case counts, and fixture hashes below are unchanged. This is owner self-verification of the named artifact and profiles, not an independent reviewer certification; the reviewer’s specific reproduction of the accounting regression is recorded separately from the profile verification claim.

## Earlier certification checkpoint (historical)

The earlier reference artifact at `d9fb4e3cdebf8a18caa4e14060ac22208a1ea4a6`, source archive SHA-256 `7280fcb919a761f3fb7add8b7cfa7c2e8f7de62886ee9979786345216e2fcb48`, passed 365 tests in the earlier full-suite runs. Registry correction commit `7cdb3615dfaa8fa3c9e807917d19029015019f14` changed metadata only at that stage. Those results are historical and are superseded as the current certification binding by `a45eaa863a7d2afb0847ce81d0952737abdf98fa`.

## Required profile IDs and exact fixture hashes

| Profile | Cases | Fixture SHA-256 |
|---|---:|---|
| `decision-surface-v1` | 15 | `6553a1bced5ccab8c4c4f14d2f8a7c255383c2d5342a9a0fae375eab92a3e8da` |
| `sp101-adversarial-input-v1` | 14 | `f9655ab7681edb5423cebfc39685ef1622c914009939bb2cba1e29e5028183d7` |
| `decision-result-hardening-v1` | 31 | `84fd28ad0d96608265198b6afa43a01555c4d7d6dd75f934822345883e6ae8d2` |
| `runtime-adversarial-v1` | 22 | `61243c33a666368e6246a2adfa500fe4658984c3156a4fba1350c78dec29b1b2` |

The official profile manifest also records exact byte counts, fixture paths, counts, sealing methods, and applicability. The decision-surface profile preserves its published fixture bytes and uses detached vector seals; other listed profiles carry per-vector `vector_hash` seals.

## Tested adversarial surface

The runtime profile exercises fail-closed behavior for unavailable policy/daemon service and estimator failures; malformed/non-finite inputs and responses; hostile decision-producer output; replay and one-shot, call-bound execution capabilities; atomic budget/repeat enforcement under tested concurrency; human-approval authentication, binding, expiration, replay, single-waiter behavior, and reservation handling; authenticated alternate approval paths; and a tested decorator escape-surface reduction.

This record does not claim exhaustive exploration of those classes. In particular, hostile OS/kernel/hypervisor control and arbitrary hostile code already executing in the same process are out of scope, along with other explicit exclusions in `CERTIFICATION.md`.

## Exact claim

> The identified implementation, version, immutable source commit, artifact digest, and tested configuration passed every required, officially published, SHA-256-pinned conformance and runtime-adversarial profile named in its registry entry under the recorded reproducible test procedure on the stated verification date. The finding applies only to that tested artifact, configuration, interfaces, and fixture hashes.

This is scoped security assurance, not an absolute guarantee of safety or absence of vulnerabilities. Owner self-verification is the publication bar; independent parties are invited to reproduce from the published immutable artifacts after publication.
