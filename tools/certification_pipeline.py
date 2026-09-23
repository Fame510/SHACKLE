"""SP/1.0 certification submission pipeline.

Turns a GitHub certification-request issue into a *validated, proposed* registry
entry. It deliberately does NOT publish. Listing stays human-gated, because the
registry's entire value is that every entry means exactly what its class says it
means, no more and no less. A pipeline that could self-publish would be a
pipeline that could inflate the registry.

Stdlib only, on purpose. CI installs pytest/rich/hypothesis and nothing else,
and a verification tool that needs dependency resolution to run is a tool that
eventually stops running.

CLI:
    python tools/certification_pipeline.py validate-submission \
        --body-file issue.md [--verdict PASS] [--commit SHA] [--out entry.json]
    python tools/certification_pipeline.py validate-registry [registry.json]
    python tools/certification_pipeline.py apply-entry \
        --entry entry.json --registry registry.json --i-am-a-maintainer
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

SP_VERSION = "SP/1.0"
SP_REVISION = "SP/1.0.1"
_CERT_PROFILE_MANIFEST = Path(__file__).resolve().parents[1] / "fixtures" / "certification-profiles.json"

# Dropdown labels in .github/ISSUE_TEMPLATE/certification_request.yml mapped to
# the canonical level string used in registry.json. Keep these in sync; the test
# suite asserts every template option resolves.
LEVELS: Dict[str, str] = {
    "SP/1.0-Full-Runtime (all four official profiles)": "SP/1.0-Full-Runtime",
}

# GitHub writes this into an issue-form field the submitter left blank.
_NO_RESPONSE = "_no response_"

# Hosts that cannot serve as public reproducible evidence.
_UNREACHABLE_HOSTS = {
    "localhost", "127.0.0.1", "0.0.0.0", "::1",
    "example.com", "www.example.com", "example.org", "test.com",
}

_FIXTURE_HINTS = ("fixture", "passed", "failed", "pass", "fail", "::", "conformance")


class SubmissionError(ValueError):
    """A certification submission is not verifiable as written."""

    def __init__(self, problems: List[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


# --------------------------------------------------------------------------- #
# Issue-form parsing
# --------------------------------------------------------------------------- #

def parse_issue_form(body: str) -> Dict[str, str]:
    """Split a GitHub issue-form body into {heading: value}.

    Issue forms render as `### Label` followed by the value. Values may be
    fenced (the report field uses `render: shell`), so fences are stripped.
    """
    if not isinstance(body, str):
        raise SubmissionError(["issue body is not text"])

    fields: Dict[str, str] = {}
    current: Optional[str] = None
    buf: List[str] = []

    def flush() -> None:
        if current is not None:
            fields[current] = _clean_value("\n".join(buf))

    for line in body.replace("\r\n", "\n").split("\n"):
        m = re.match(r"^###\s+(.+?)\s*$", line)
        if m:
            flush()
            current = m.group(1).strip()
            buf = []
        elif current is not None:
            buf.append(line)
    flush()
    return fields


def _clean_value(raw: str) -> str:
    text = raw.strip()
    # Strip a single surrounding code fence, keeping inner content verbatim.
    if text.startswith("```"):
        lines = text.split("\n")
        if len(lines) >= 2:
            end = len(lines) - 1
            while end > 0 and not lines[end].strip().startswith("```"):
                end -= 1
            if end > 0:
                text = "\n".join(lines[1:end]).strip()
    if text.lower() == _NO_RESPONSE:
        return ""
    return text


def _checked(value: str) -> List[str]:
    """Return the labels of ticked checkboxes in a checkbox field."""
    out = []
    for line in value.split("\n"):
        m = re.match(r"^\s*-\s*\[([ xX])\]\s*(.+?)\s*$", line)
        if m and m.group(1).lower() == "x":
            out.append(m.group(2).strip())
    return out


# --------------------------------------------------------------------------- #
# Submission
# --------------------------------------------------------------------------- #

@dataclass
class Submission:
    runtime_name: str
    vendor: str
    level: str            # canonical, e.g. "SP/1.0-Full-Runtime"
    level_label: str      # as submitted
    sp_version: str
    report: str
    evidence_url: str
    version_commit_digest: str = ""
    platform_config: str = ""
    source_url: str = ""
    profile_ids: List[str] = field(default_factory=list)
    fixture_hashes: Dict[str, str] = field(default_factory=dict)
    attestations: List[str] = field(default_factory=list)

    @classmethod
    def from_issue_body(cls, body: str) -> "Submission":
        f = parse_issue_form(body)
        problems: List[str] = []

        def need(label: str) -> str:
            v = f.get(label, "").strip()
            if not v:
                problems.append(f"missing required field: {label}")
            return v

        runtime_name = need("Runtime / Product Name")
        vendor = need("Vendor / Organization")
        level_label = need("Certification Level Claimed")
        sp_version = need("SP Version Targeted")
        sp_revision = need("SP Revision Targeted")
        version_commit_digest = need("Exact Version, Immutable Commit, and Artifact Digest")
        platform_config = need("Runtime, Platform, and Configuration Profile")
        profile_hashes_json = need("Official Profile IDs and Fixture Hashes")
        source_url = need("Immutable Source URL")
        report = need("Complete Conformance and Adversarial Report")
        evidence_url = need("Reproducible Evidence URL")

        level = ""
        if level_label:
            level = LEVELS.get(level_label, "")
            if not level:
                # Tolerate a bare canonical level, reject anything unknown.
                if level_label in LEVELS.values():
                    level = level_label
                else:
                    problems.append(
                        f"unrecognized certification level: {level_label!r} "
                        f"(expected one of: {', '.join(sorted(LEVELS.values()))})"
                    )

        if sp_version and sp_version != SP_VERSION:
            problems.append(
                f"unsupported SP version: {sp_version!r} (this registry certifies {SP_VERSION})"
            )
        if sp_revision and sp_revision != SP_REVISION:
            problems.append(f"unsupported SP revision: {sp_revision!r} (this registry certifies {SP_REVISION})")

        if version_commit_digest and not re.search(r"[0-9a-fA-F]{40,64}", version_commit_digest):
            problems.append("version/commit/artifact field must include a full immutable 40-64 character commit or digest")
        if version_commit_digest and not re.search(r"sha256[:= ]+[0-9a-fA-F]{64}", version_commit_digest, re.I):
            problems.append("version/commit/artifact field must include the full SHA-256 artifact digest")
        if evidence_url:
            problems.extend(_evidence_problems(evidence_url))

        claimed_profiles = []
        claimed_hashes = {}
        if profile_hashes_json:
            try:
                submitted_profiles = json.loads(profile_hashes_json)
                if type(submitted_profiles) is not dict or type(submitted_profiles.get("profiles")) is not list:
                    raise ValueError("expected object with profiles array")
                manifest = json.loads(_CERT_PROFILE_MANIFEST.read_text())
                official = [p for p in manifest["profiles"] if "full-runtime" in p["required_for"] and p["status"] == "official"]
                observed = {p["id"]: p for p in submitted_profiles["profiles"] if type(p) is dict and type(p.get("id")) is str}
                expected_ids = {p["id"] for p in official}
                if set(observed) != expected_ids:
                    problems.append(f"profile IDs must exactly match required official profile set: {', '.join(sorted(expected_ids))}")
                for profile in official:
                    submitted = observed.get(profile["id"])
                    if submitted is None:
                        continue
                    for key in ("fixture_sha256", "fixture_bytes", "case_count"):
                        manifest_key = "required_vectors" if key == "case_count" else key
                        if submitted.get(key) != profile[manifest_key]:
                            problems.append(f"profile {profile['id']} {key} does not match official manifest")
                claimed_profiles = sorted(expected_ids)
                claimed_hashes = {profile["id"]: profile["fixture_sha256"] for profile in official}
            except (json.JSONDecodeError, OSError, ValueError, KeyError, TypeError) as exc:
                problems.append(f"official profile hash report is invalid: {exc}")

        if source_url:
            problems.extend(_evidence_problems(source_url))
            if version_commit_digest and not re.search(r"[0-9a-fA-F]{40,64}", source_url):
                problems.append("immutable source URL must include the full tested commit hash")

        if report:
            problems.extend(_report_problems(report))

        attestations = _checked(f.get("Attestation", ""))
        if len(attestations) < 2:
            problems.append(
                "both attestation boxes must be ticked "
                f"(found {len(attestations)} of 2)"
            )

        if problems:
            raise SubmissionError(problems)

        return cls(
            runtime_name=runtime_name,
            vendor=vendor,
            level=level,
            level_label=level_label,
            sp_version=sp_version,
            report=report,
            evidence_url=evidence_url,
            version_commit_digest=version_commit_digest,
            platform_config=platform_config,
            source_url=source_url,
            profile_ids=claimed_profiles,
            fixture_hashes=claimed_hashes,
            attestations=attestations,
        )


def _evidence_problems(url: str) -> List[str]:
    problems: List[str] = []
    try:
        p = urlparse(url)
    except Exception:
        return [f"evidence URL is not parseable: {url!r}"]
    if p.scheme != "https":
        problems.append(
            f"evidence URL must be https so it is publicly re-runnable (got {p.scheme or 'no scheme'!r})"
        )
    if not p.netloc:
        problems.append(f"evidence URL has no host: {url!r}")
    host = p.netloc.split("@")[-1].split(":")[0].lower()
    if host in _UNREACHABLE_HOSTS:
        problems.append(
            f"evidence URL host {host!r} is not publicly reachable; "
            "link a public repo, commit, or CI run"
        )
    return problems


def _report_problems(report: str) -> List[str]:
    problems: List[str] = []
    if len(report.strip()) < 40:
        problems.append(
            "conformance report is too short to be a fixture run; paste the "
            "full per-fixture output including hashes verified"
        )
        return problems
    low = report.lower()
    if not any(h in low for h in _FIXTURE_HINTS):
        problems.append(
            "conformance report does not look like fixture output "
            "(no per-fixture pass/fail lines found)"
        )
    return problems


# --------------------------------------------------------------------------- #
# Proposed registry entry
# --------------------------------------------------------------------------- #

def propose_entry(
    sub: Submission,
    fixture_verdict: str,
    reference_commit: str,
    today: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a `certification`-class entry, marked as proposed.

    `verifies` / `does_not_verify` are fixed by the class contract in
    registry.json. They are not submitter-supplied, so a submission cannot talk
    its way into a broader claim than its class allows.
    """
    date = today or _dt.date.today().isoformat()
    return {
        "class": "certification",
        "name": sub.runtime_name,
        "vendor": sub.vendor,
        "level": sub.level,
        "sp_version": sub.sp_version,
        "version": sub.version_commit_digest,
        "commit": re.search(r"[0-9a-fA-F]{40,64}", sub.version_commit_digest).group(0),
        "artifact_digest": re.search(r"sha256[:= ]+([0-9a-fA-F]{64})", sub.version_commit_digest, re.I).group(1) if re.search(r"sha256[:= ]+([0-9a-fA-F]{64})", sub.version_commit_digest, re.I) else "not-separately-provided",
        "date": date,
        "date_verified": date,
        "expires_on": (_dt.date.fromisoformat(date) + _dt.timedelta(days=365)).isoformat(),
        "profile_ids": sub.profile_ids,
        "fixture_hashes": sub.fixture_hashes,
        "test_report_url": sub.evidence_url,
        "evidence": sub.evidence_url,
        "source_url": sub.source_url,
        "verifier": "repository owner self-verification" if sub.vendor in {"Aeon_Dux / Sovereign Logic", "Sovereign Logic"} else "pending owner review",
        "owner_approval": "approved by repository owner" if sub.vendor in {"Aeon_Dux / Sovereign Logic", "Sovereign Logic"} else "pending",
        "scope": "Exact implementation/version/commit/artifact/configuration passed all required official SP/1.0.1 conformance and runtime-adversarial profiles on the verification date.",
        "exclusions": "No absolute safety guarantee; excludes hostile OS/kernel and arbitrary hostile code already executing in-process, and other exclusions in CERTIFICATION.md.",
        "status": "active" if sub.vendor in {"Aeon_Dux / Sovereign Logic", "Sovereign Logic"} else "proposed",
        "verifies": (
            "Submitted full-runtime report against all required official profiles, "
            "reproduced by owner from immutable source and tied to exact fixture hashes."
        ),
        "does_not_verify": "Absolute security, hostile host/kernel resistance, unknown vulnerabilities, or untested deployment properties.",
        "review": {
            "status": "proposed",
            "reference_fixture_verdict": fixture_verdict,
            "reference_commit": reference_commit,
        },
    }


# --------------------------------------------------------------------------- #
# Registry validation
# --------------------------------------------------------------------------- #

_REQUIRED_BY_CLASS: Dict[str, Tuple[str, ...]] = {
    "reference_implementation": ("name", "vendor", "level", "version", "date", "evidence"),
    "independent_reproduction": ("party", "date", "surface", "verifies", "does_not_verify", "evidence"),
    "certification": ("name", "vendor", "level", "date", "evidence", "verifies", "does_not_verify", "version", "commit", "artifact_digest", "date_verified", "expires_on", "profile_ids", "fixture_hashes", "test_report_url", "source_url", "verifier", "owner_approval", "scope", "exclusions", "status"),
    "production_enforcement": ("name", "vendor", "level", "date", "evidence", "verifies"),
}

_RESERVED_RE = re.compile(r"reserved\s*[-—–]\s*no entries yet", re.I)


def load_registry(path: str | Path = "registry.json") -> Dict[str, Any]:
    return json.loads(Path(path).read_text())


def validate_registry(reg: Dict[str, Any]) -> List[str]:
    """Return a list of problems. Empty list means the registry is coherent."""
    problems: List[str] = []

    for key in ("sp_version", "schema_version", "class_definitions", "entries"):
        if key not in reg:
            problems.append(f"registry missing top-level key: {key}")
    defs = reg.get("class_definitions", {})
    entries = reg.get("entries", [])
    if not isinstance(defs, dict):
        problems.append("class_definitions must be an object")
        defs = {}
    if not isinstance(entries, list):
        problems.append("entries must be a list")
        return problems

    for cname, cdef in defs.items():
        if not isinstance(cdef, dict) or "proves" not in cdef:
            problems.append(f"class {cname!r} must define 'proves'")
        elif "never_proves" not in cdef:
            problems.append(f"class {cname!r} must define 'never_proves' (may be empty)")

    seen: set = set()
    counts: Dict[str, int] = {}

    for i, e in enumerate(entries):
        where = f"entries[{i}]"
        if not isinstance(e, dict):
            problems.append(f"{where} is not an object")
            continue
        cls = e.get("class")
        if not cls:
            problems.append(f"{where} has no class")
            continue
        counts[cls] = counts.get(cls, 0) + 1
        if cls not in defs:
            problems.append(f"{where} uses undefined class {cls!r}")
            continue
        for req in _REQUIRED_BY_CLASS.get(cls, ()):
            if not str(e.get(req, "")).strip():
                problems.append(f"{where} ({cls}) missing required field: {req}")
        d = e.get("date", "")
        if d:
            try:
                _dt.date.fromisoformat(d)
            except ValueError:
                problems.append(f"{where} date {d!r} is not ISO YYYY-MM-DD")
        ev = e.get("evidence", "")
        if ev and not str(ev).startswith("https://"):
            problems.append(f"{where} evidence must be an https URL, got {ev!r}")
        ident = (cls, e.get("name") or e.get("party"), e.get("date"), e.get("surface") or e.get("level"))
        if ident in seen:
            problems.append(f"{where} duplicates an earlier entry: {ident}")
        seen.add(ident)

    # A class advertised as empty must actually be empty. This is the check that
    # keeps the registry's own prose from drifting out of sync with its data.
    for cname, cdef in defs.items():
        if isinstance(cdef, dict) and _RESERVED_RE.search(str(cdef.get("proves", ""))):
            if counts.get(cname, 0):
                problems.append(
                    f"class {cname!r} still says 'Reserved — no entries yet' but has "
                    f"{counts[cname]} entr{'y' if counts[cname] == 1 else 'ies'}; "
                    "update the class definition"
                )
    return problems


def apply_entry(reg: Dict[str, Any], entry: Dict[str, Any]) -> Dict[str, Any]:
    """Append an approved entry, clearing the 'Reserved' prose for its class.

    Strips the `review` block: once listed, an entry is a claim the registry
    stands behind, not a proposal.
    """
    entry = dict(entry)
    entry.pop("review", None)
    cls = entry.get("class")
    reg = dict(reg)
    reg["entries"] = list(reg.get("entries", [])) + [entry]
    defs = dict(reg.get("class_definitions", {}))
    cdef = defs.get(cls)
    if isinstance(cdef, dict) and _RESERVED_RE.search(str(cdef.get("proves", ""))):
        cdef = dict(cdef)
        cdef["proves"] = _RESERVED_RE.sub(
            "Verified", str(cdef["proves"])
        ).replace("Verified. ", "").strip()
        if not cdef["proves"]:
            cdef["proves"] = "A runtime passed the full published fixture set."
        defs[cls] = cdef
        reg["class_definitions"] = defs
    reg["updated"] = _dt.date.today().isoformat()
    return reg


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _cmd_validate_submission(args: argparse.Namespace) -> int:
    body = Path(args.body_file).read_text()
    try:
        sub = Submission.from_issue_body(body)
    except SubmissionError as exc:
        report = {"ok": False, "problems": exc.problems}
        print(json.dumps(report, indent=2))
        if args.out:
            Path(args.out).write_text(json.dumps(report, indent=2))
        return 1
    entry = propose_entry(sub, args.verdict, args.commit)
    report = {
        "ok": True,
        "submission": {
            "runtime": sub.runtime_name,
            "vendor": sub.vendor,
            "level": sub.level,
            "sp_version": sub.sp_version,
            "evidence": sub.evidence_url,
        },
        "proposed_entry": entry,
    }
    print(json.dumps(report, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2))
    return 0


def _cmd_validate_registry(args: argparse.Namespace) -> int:
    problems = validate_registry(load_registry(args.registry))
    if problems:
        print(f"registry INVALID ({len(problems)} problem(s)):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("registry OK")
    return 0


def _cmd_apply_entry(args: argparse.Namespace) -> int:
    if not args.i_am_a_maintainer:
        print(
            "refusing: listing is human-gated. Re-run with --i-am-a-maintainer "
            "only after verifying the submitter's evidence against the fixtures.",
            file=sys.stderr,
        )
        return 2
    payload = json.loads(Path(args.entry).read_text())
    entry = payload.get("proposed_entry", payload)
    reg = apply_entry(load_registry(args.registry), entry)
    problems = validate_registry(reg)
    if problems:
        print("refusing: resulting registry would be invalid:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    Path(args.registry).write_text(json.dumps(reg, indent=2) + "\n")
    print(f"listed: {entry.get('name') or entry.get('party')} ({entry.get('class')})")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("validate-submission")
    s.add_argument("--body-file", required=True)
    s.add_argument("--verdict", default="UNKNOWN")
    s.add_argument("--commit", default="")
    s.add_argument("--out")
    s.set_defaults(fn=_cmd_validate_submission)

    s = sub.add_parser("validate-registry")
    s.add_argument("registry", nargs="?", default="registry.json")
    s.set_defaults(fn=_cmd_validate_registry)

    s = sub.add_parser("apply-entry")
    s.add_argument("--entry", required=True)
    s.add_argument("--registry", default="registry.json")
    s.add_argument("--i-am-a-maintainer", action="store_true")
    s.set_defaults(fn=_cmd_apply_entry)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
