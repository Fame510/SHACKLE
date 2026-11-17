# Contributing to SHACKLE

Thanks for your interest in improving SHACKLE. This repository holds the
SHACKLE runtime governance implementation (`shackle`, published to PyPI as
`pyshackle`), the **SP/1.0 specification** (`SP-1.0-SPECIFICATION.md`), and the
certification and conformance machinery that keeps both honest.

## Ways to contribute

- **Bug reports**: open a GitHub Issue. Use the provided issue templates where
  they apply. Include the SHACKLE version, the framework and version you are
  running (CrewAI, LangChain/LangGraph, AutoGen, Smolagents, LiteLLM), and the
  exact call path involved.
- **Coverage questions**: SHACKLE's governance hooks are integration- and
  path-dependent. Before filing, check `INTEGRATIONS.md` and
  `Guard.last_coverage`; if you hit a path that is not covered, a minimal
  reproduction is the most useful report you can file.
- **Code changes**: pull requests against `master` (see below).
- **Spec feedback**: the SP/1.0 specification is versioned and published. Open
  an Issue describing the gap or ambiguity, referencing the section, before
  proposing wording changes. Conformance behavior is pinned by the
  [CONFORMANCE.md](CONFORMANCE.md) process and the public 82-case battery in
  `fixtures/` — changes to either must be explicit, not incidental.

## Development setup

```bash
git clone https://github.com/Fame510/SHACKLE.git
cd SHACKLE
python -m venv .venv && source .venv/bin/activate   # Python 3.10+
pip install -e .
pytest tests/
```

CI runs the same suites on every pull request (`.github/workflows/ci.yml`);
the certification workflow (`.github/workflows/certification-verify.yml`)
re-verifies the published artifact binding.

## Pull request expectations

1. **Keep it focused.** One behavioral change or one documentation change per
   PR, with a clear title and a description that says what changed and why.
2. **Tests travel with behavior.** New behavior or a fixed bug needs a test in
   `tests/` that fails without your change and passes with it. The public
   conformance battery must keep passing; if your change affects certified
   behavior, say so in the PR and expect extra review.
3. **Match the honesty standard.** This project's public claims are scoped to
   what is tested. PR descriptions and docs changes must not claim framework
   coverage, billing accuracy, or security properties beyond what the test
   suite demonstrates. `INTEGRATIONS.md` records exact coverage limits; update
   it when you change an integration path.
4. **Style.** Python 3.10+, type hints on public interfaces, and the existing
   module layout in `shackle/`. Keep generated artifacts (badges, registry
   JSON) out of hand edits unless the change is to the artifact itself.

## Licensing

- Code contributions are made under **AGPL-3.0**, matching [LICENSE](LICENSE).
  By opening a PR you confirm you have the right to contribute the work under
  those terms.
- Specification and documentation content is licensed **CC BY 4.0** under
  [LICENSE-SPEC.md](LICENSE-SPEC.md); spec-side PRs follow the same rules.

## Questions

Open a GitHub Issue. For security-sensitive reports, do **not** open a public
issue — see [SECURITY.md](SECURITY.md) instead.
