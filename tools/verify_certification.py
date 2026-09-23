#!/usr/bin/env python3
"""Reproducibly verify local official profiles and emit evidence for a pinned commit.

Run after the intended source commit exists: python tools/verify_certification.py
"""
from __future__ import annotations
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "fixtures/certification-profiles.json"

def main() -> int:
    manifest = json.loads(MANIFEST.read_text())
    if manifest["schema"] != "shackle-certification-profile-manifest-v1":
        raise SystemExit("unsupported profile manifest")
    rows = []
    for profile in manifest["profiles"]:
        if "full-runtime" not in profile["required_for"] or profile["status"] != "official":
            continue
        raw = (ROOT / profile["fixture"]).read_bytes()
        got = hashlib.sha256(raw).hexdigest()
        if got != profile["fixture_sha256"] or len(raw) != profile["fixture_bytes"]:
            raise SystemExit(f"profile integrity mismatch: {profile['id']}")
        parsed = json.loads(raw)
        vectors = parsed["fixtures"]
        if len(vectors) != profile["required_vectors"]:
            raise SystemExit(f"profile count mismatch: {profile['id']}")
        rows.append({"id": profile["id"], "fixture_sha256": got,
                     "fixture_bytes": len(raw), "case_count": len(vectors)})
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=short"], cwd=ROOT, text=True)
    if run.returncode:
        return run.returncode
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                            capture_output=True, text=True).stdout.strip()
    print(json.dumps({"status": "PASS", "commit": commit, "profiles": rows}, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
