# SHACKLE reference runtime verification record

**Verification date:** 2026-09-23  
**Tested implementation commit:** `d9fb4e3cdebf8a18caa4e14060ac22208a1ea4a6`  
**Source archive SHA-256:** `7280fcb919a761f3fb7add8b7cfa7c2e8f7de62886ee9979786345216e2fcb48`  
**Registry correction commit:** `7cdb3615dfaa8fa3c9e807917d19029015019f14` (metadata-only registry update; no runtime source changes)  
**Environment:** Python 3.12.12; Linux 6.1.158+ x86_64  
**Verifier:** repository owner, local self-verification

## Result

Fresh full-suite runs were made against the tested implementation commit and again against `master` after the registry binding correction:

```text
Tested implementation commit d9fb4e3cdebf8a18caa4e14060ac22208a1ea4a6:
365 passed, 34 warnings in 9.84s

Post-registry-correction master commit 7cdb3615dfaa8fa3c9e807917d19029015019f14:
365 passed, 34 warnings in 9.37s
```

The 34 warnings were existing deprecations from licensing/FastAPI/Redis test paths; there were no failed or skipped tests. The suite includes conformance, daemon, approval-flow, malformed-boundary, replay/capability, race/budget, certification-pipeline, and adversarial tests. Profile file and vector seals are checked by the tests. The certification entry binds to the immutable implementation source commit above; its source archive digest is computed from that commit. The later registry correction changed metadata only and points to that tested source commit.

The registry validation also passed on the post-correction master tree:

```text
python tools/certification_pipeline.py validate-registry registry.json
registry OK
```

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
