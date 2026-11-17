# Changelog

Notable changes to the SHACKLE runtime (`pyshackle` on PyPI) and the SP/1.0
specification are recorded here. Dates and artifact bindings are drawn from
the repository's publication record in [PROVENANCE.md](PROVENANCE.md); the
certification registry and its verification report bind claims to exact
commits.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

## [1.0.0] - 2026-07-20

First release of `pyshackle` on PyPI (2026-07-20).

Project milestones leading to this release, from the publication record:

- 2026-06-17: first release commit (`9fbf7c3a`).
- 2026-06-18: v2 foundation committed.
- 2026-06-24: SP/1.0 specification published.
- 2026-07-30: SP/1.0.1 implementation tightening.

### Runtime hardening verified 2026-09-23

Certification verification (2026-09-23) binds the owner-verified reference v2
runtime to commit `a45eaa863a7d2afb0847ce81d0952737abdf98fa` with the four
official hash-pinned profiles: 15 + 14 + 31 + 22 = 82 cases. Changes shipped
and regression-tested on that line:

- **Model-aware pricing resolution**: exact model IDs, provider prefixes, and
  delimiter-bounded dated variants resolve to known pricing rows; unknown
  models use the default row with a warning.
- **Terminal-abort latching**: terminal aborts survive framework
  exception-swallowing paths and are surfaced at the `Guard` boundary.
- **Pre-call gates**: known elapsed-time and exhausted-budget checks run
  before further requests on supported LLM paths.
- **Two-layer LiteLLM interception**, including a call-time gate for
  early-bound imports.
- **Native CrewAI hooks**: before-tool, before-LLM, and after-LLM.
- **Coverage-gap detection** with optional `Guard(strict=True)` refusal to
  run when detected gaps remain.

See [PROVENANCE.md](PROVENANCE.md) for dates, commits, attribution, and
claim boundaries.
