# SHACKLE Runtime Certification Policy

**Status:** Official SHACKLE runtime certification policy. The reference implementation is self-verified by the repository owner against the required pinned profiles before the corresponding registry listing is published. **Specification:** SP/1.0.1; this policy does not change the specification revision.

## Certification claim

A SHACKLE runtime certification is a version-specific, dated security-assurance finding. It means exactly this:

> **The identified implementation, version, immutable source commit, artifact digest, and tested configuration passed every required, officially published, SHA-256-pinned conformance and runtime-adversarial profile named in its registry entry under the recorded reproducible test procedure on the stated verification date. The finding applies only to that tested artifact, configuration, interfaces, and fixture hashes.**

This is a real claim about security properties exercised by those tests—not a claim of absolute safety, zero vulnerabilities, or invulnerability. “Court-proof,” “unbreakable,” and unqualified “secure and safe” are not certification language. The report is evidence of identified tests and observed results, not a legal conclusion, warranty, or guarantee about unknown attacks.

The repository owner owns the certification program, policy, publication of official fixture profiles, registry, and final listing decision. An independent reviewer such as nutstrut may choose to reproduce a published profile and submit observations. That is a separate reviewer role, not ownership, authorship, or a prerequisite to the owner publishing a profile. External implementations are invited to run the public tests independently after release and submit reproducible results.

## Required full-runtime battery

A full-runtime certification requires every profile marked `required_for: ["full-runtime"]` in `fixtures/certification-profiles.json`:

1. `decision-surface-v1` — 15 SP/1.0 decision vectors; the fixture bytes are frozen and the vectors have detached seals.
2. `sp101-adversarial-input-v1` — 14 SP/1.0.1 decision input vectors with whole-vector seals.
3. `decision-result-hardening-v1` — 31 official decision-result vectors with whole-vector seals.
4. `runtime-adversarial-v1` — 22 official runtime regression cases, with whole-file and per-case SHA-256 seals, tied to tests in the reference Python v2 runtime.

Every profile has a stable ID, exact source file, byte count, whole-file SHA-256, expected case count, sealing method, and applicability statement. The manifest is authoritative. Published fixture files are immutable: corrections or additions require a new profile ID/version, preserving the prior bytes and hashes. A profile is not official unless its manifest entry says `official` and the tests verify its hash and all applicable per-vector seals.

Self-verification by the SHACKLE repository owner against this pinned suite is the publication/registry bar for SHACKLE's own reference implementation. No outside review is required before publication. Once published, third parties can independently fetch the exact artifacts, run the public command, confirm every hash, and report their results. Independent reproductions are recorded separately from the owner's certification decision.

## Tested runtime attack classes

`runtime-adversarial-v1` and its referenced test suite exercise these specific cases:

- daemon/policy authority unavailable, malformed, or returning an invalid decision: execution fails closed;
- thrown, malformed, negative, or non-finite cost estimates: no zero-cost fallback and no wrapped tool dispatch;
- malformed request/configuration boundary values, adversarial Python container subclasses, oversized strings/node counts, and malformed Redis atomic results;
- hostile decision producer output, including monkeypatched malformed ALLOW-shaped results, normalized to a restrictive verdict;
- same-request replay denial, execution capability binding to session/request/call parameters, one-shot consumption, and duplicate post-execution rejection;
- Redis atomic budget reservation, concurrent distinct requests, repeat-call ceilings, and prevention of concurrent overspend;
- HITL authentication separation, token/session/request binding, expiration and bounded queue, single waiter, response replay denial, and notification-only WebSocket behavior;
- tested wrapper escape surface removal and the alternate authenticated HTTP route for approval changes.

These statements name the cases exercised by the published tests. They do not imply exhaustive testing of every implementation or every possible attack in a class. A certification report must include profile IDs and exact hashes, source commit and artifact digest, environment/toolchain, command, raw result, and every skip or failure.

## Scope and exclusions

The runtime profile targets the reference Python v2 daemon/client and the tested integration boundaries. A third-party implementation must identify the equivalent tested surface and provide a reproducible mapping from its behavior to each required vector. Decision-only implementations may report decision-surface conformance, but may not claim full-runtime certification.

Certification does **not** establish protection against:

- hostile OS, kernel, hypervisor, container host/runtime, or a compromised deployment platform;
- arbitrary hostile code already executing in the same Python process, including private-object/frame introspection, deliberate guard removal, or arbitrary monkeypatching after authorization;
- compromised Redis/PostgreSQL administrators, secrets/signing-key custody, dependency supply chain, build pipeline, malicious tools/providers, alternate credentials, or unmediated paths outside the tested integration surface;
- timing/microarchitectural side channels, denial of service, all possible race schedules, or untested production topology;
- attacks not explicitly covered by an official profile, absence of unknown vulnerabilities, legal outcomes, or regulatory compliance.

Those environments are not simulated by this battery and are exclusions, not passes. Where a credible bypass is demonstrated within the certified tested scope, the listing must be suspended pending investigation and fresh verification.

## Submission report and review

Submissions must provide:

- implementation/product and vendor; exact version, immutable source commit, and artifact/package digest;
- language, runtime/platform versions, OS/container image, dependencies, and non-secret configuration;
- every claimed profile ID, fixture SHA-256, byte count, and case count;
- for every case: expected vs observed verdict/reason, PASS/FAIL/UNSUPPORTED, and diagnostic reference;
- explicit disclosures of deviations, patches, unsupported vectors, skips, environmental limits, dependencies, and known bypasses;
- a public, reproducible source/build workflow, runnable test/report command, raw output, date, and submitter attestation.

A “passed” summary without case-level results, exact hashes, or a runnable reproduction is incomplete. The issue form is intake only; it never automatically grants a listing. The owner reviews the report and determines whether the evidence supports the exact proposed registry claim. Nutstrut and other independent reviewers can reproduce after publication and submit a separate reproduction record; no reviewer is assigned work or made an owner by this policy.

Full certification requires every required vector/test to pass, all artifacts to match their published seals, no unexplained skip or unsupported required vector, and no unresolved critical/high-severity in-scope finding. Partial results are recorded as partial reports, never as full certification.

## Registry and lifecycle

The registry entry binds the claim to `name`, `vendor`, `version`, `commit`, `artifact_digest`, `date_verified`, `expires_on`, `profile_ids`, `fixture_hashes`, `test_report_url`, `source_url`, `verifier`, `owner_approval`, `scope`, `exclusions`, and `status`. The first full-runtime entry is the SHACKLE reference implementation, tested by the owner against the exact profile hashes recorded in its entry.

Any source, build, or configuration change affecting a tested path requires a new verification for the new immutable commit/artifact. Reverify at least annually, on a required-profile change, or after a credible in-scope vulnerability. Mark old records `superseded`, `expired`, `suspended`, or `revoked`; preserve history, dates, reasons, and evidence rather than silently rewriting a claim. Certification does not transfer to a version string or product name.

## Repository artifacts

- `CERTIFICATION.md` — policy, claim scope, test procedure, exclusions, and lifecycle.
- `fixtures/certification-profiles.json` — official profile IDs, hashes, counts, and applicability.
- `fixtures/runtime-adversarial-v1.json` — immutable 22-case runtime regression profile.
- `tests/test_adversarial_hardening.py` and `v2/daemon/test_state_atomic.py` — reproducible reference runtime checks.
- `registry.json` — schema and evidence-bound reference certification entry.
- `tools/verify_certification.py` — checks official profile hashes/counts, runs the full suite, and emits the local verification commit/profile report.
- `.github/ISSUE_TEMPLATE/certification_request.yml` — third-party evidence intake and reproducibility details.
