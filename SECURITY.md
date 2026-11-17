# Security Policy

## Supported versions

| Component | Support |
|---|---|
| `pyshackle` 1.0.x (PyPI) | Supported: report vulnerabilities against the latest 1.0.x release |
| `v2/` sidecar runtime | In development; reports welcome, expect API churn |

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** for anything that could
expose users or their workloads: in this repository, open the **Security** tab
and choose **Report a vulnerability**. Reports arrive privately to the
maintainer and are not visible publicly until a fix and disclosure are agreed.

What helps us act quickly:

- A short description of the vulnerability and its impact.
- The component and code path involved (for example: Guard budget gate,
  LiteLLM interception layer, CrewAI hooks, certification verifier).
- The `pyshackle` version or the exact commit you tested against.
- A minimal reproduction (script, fixture, or sequence of calls).
- Any known workaround.

Please do not open a public GitHub Issue, and please do not test against
systems or accounts you do not own, while a report is being triaged.

## Scope

In scope: this repository's code and published artifacts — the `shackle`
runtime, the `Guard` enforcement layer and its integration hooks, the policy
decision logic, the certification and verification tooling under `tools/`, and
the SP/1.0 specification text where an ambiguity creates a security-relevant
conformance gap.

Out of scope: vulnerabilities in upstream frameworks or provider SDKs (report
those to their maintainers; we will help with a workaround where SHACKLE can
provide one), and provider billing differences — SHACKLE's spend accounting is
an estimate by design, and the repository's published claims describe its
limits. Where a report shows our documentation overclaims, that is in scope:
the honesty of the claims is part of the security posture.

## Disclosure

We practice coordinated disclosure: we confirm the report, develop and test a
fix, publish it, and then acknowledge the issue publicly. Reporters who wish
to be credited are credited, in the disclosure and in
[PROVENANCE.md](PROVENANCE.md) where it records project history.
