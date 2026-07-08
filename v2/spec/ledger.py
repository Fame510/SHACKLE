"""
SHACKLE Audit Ledger - SP/1.0
=============================
Append-only, Ed25519-signed, SHA-256 hash-chained, file-backed audit ledger.

This is the *evidence* layer that sits beside the pure control core (decide()
in v2/spec/decide.py). decide() answers "should this run?" with zero I/O; this
ledger records what happened in a tamper-evident chain so an auditor can later
prove the history is complete and unaltered.

Each record embeds the hash of the previous record (prev_hash) and its own
record_hash = sha256(canonical(core) + prev_hash), forming an unbroken chain
from a fixed genesis. Removing, reordering, or editing any record breaks the
chain and is detected by verify_chain(). Records are additionally Ed25519-signed
when PyNaCl is available.

No "proof-of-receipt" workflow exists or is implied. The contract is this source.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Dict, List, Optional

try:
    from nacl.signing import SigningKey, VerifyKey
    from nacl.encoding import HexEncoder
    _HAVE_NACL = True
except Exception:  # pragma: no cover - nacl optional
    _HAVE_NACL = False

GENESIS = "0" * 64


def _canonical(obj: dict) -> str:
    """Deterministic JSON: sorted keys, compact separators, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


class AuditLedger:
    """Synchronous, dependency-light, tamper-evident audit ledger.

    Usage:
        led = AuditLedger("audit.log")
        led.log_decision("s1", "click", "ALLOW", "within_thresholds")
        assert led.verify_chain()["valid"]
    """

    def __init__(self, path: str, signing_key_hex: Optional[str] = None):
        self.path = path
        self._last_hash = GENESIS
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        self.signing_key = None
        self.verify_key_hex = ""
        if _HAVE_NACL:
            if signing_key_hex:
                self.signing_key = SigningKey(signing_key_hex, encoder=HexEncoder)
            else:
                self.signing_key = SigningKey.generate()
            self.verify_key_hex = self.signing_key.verify_key.encode(
                encoder=HexEncoder).decode()
        self._recover_last_hash()

    def _recover_last_hash(self) -> None:
        if not os.path.exists(self.path):
            return
        last = None
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    last = line
        if last:
            try:
                self._last_hash = json.loads(last)["record_hash"]
            except Exception:
                pass

    def _build_record(self, event: dict) -> dict:
        core = dict(event)
        core["prev_hash"] = self._last_hash
        record_hash = hashlib.sha256(
            (_canonical(core) + self._last_hash).encode()).hexdigest()
        core["record_hash"] = record_hash
        if self.signing_key is not None:
            core["signature"] = self.signing_key.sign(
                record_hash.encode()).signature.hex()
            core["verify_key"] = self.verify_key_hex
        else:
            core["signature"] = ""
            core["verify_key"] = ""
        return core

    def append(self, event: dict) -> dict:
        record = self._build_record(event)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        self._last_hash = record["record_hash"]
        return record

    def log_decision(self, session_id: str, tool_name: str, verdict: str,
                     reason: str = "", metadata: Optional[dict] = None) -> dict:
        return self.append({
            "ts": time.time(), "event_type": "decision", "session_id": session_id,
            "tool_name": tool_name, "verdict": verdict, "reason": reason,
            "metadata": metadata or {},
        })

    def log_execution(self, session_id: str, tool_name: str, ok: bool,
                      cost_usd: float = 0.0, error: str = "",
                      metadata: Optional[dict] = None) -> dict:
        return self.append({
            "ts": time.time(), "event_type": "execution", "session_id": session_id,
            "tool_name": tool_name, "ok": ok, "cost_usd": cost_usd, "error": error,
            "metadata": metadata or {},
        })

    def read_all(self) -> List[dict]:
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def verify_chain(
        self,
        expected_verify_key: Optional[str] = None,
        require_signatures: Optional[bool] = None,
    ) -> Dict[str, object]:
        """Recompute the chain and validate integrity.

        Returns {valid, count, broken_at, reason, signed}.

        SECURITY (fix/audit-hardening): record_hash is a keyless SHA-256, so an
        attacker who edits a record can recompute the whole chain and the hash
        chain alone still verifies. The Ed25519 signature is the only identity
        binding, so signature checking must not be skippable by blanking the
        signature fields.

        Two modes:
          * DEFAULT (backward compatible): verify the hash chain AND verify each
            record's signature against its OWN embedded verify_key. This detects
            in-place content tampering (hash mismatch) and correctly supports
            legitimate multi-instance/rotated-key chains, where each record is
            signed by whatever key was active when it was written. A record that
            carries a signature which does not verify is rejected.
          * STRICT (opt-in): pass expected_verify_key to PIN one identity — every
            record must be signed by exactly that key (defeats an attacker who
            re-signs with their own key). require_signatures (defaults True in
            strict mode, or when explicitly set) makes a missing/blank signature
            a hard failure, so signatures can no longer be stripped to pass.
        """
        pin = expected_verify_key  # ONLY an explicit pin enforces one identity
        if require_signatures is None:
            # Strict signature presence is required when an identity is pinned.
            require_signatures = pin is not None

        if require_signatures and not _HAVE_NACL:
            return {"valid": False, "count": 0, "broken_at": None, "signed": False,
                    "reason": "signatures required but PyNaCl is unavailable to verify them"}

        records = self.read_all()
        prev = GENESIS
        any_signed = False
        for i, rec in enumerate(records):
            core = {k: v for k, v in rec.items()
                    if k not in ("record_hash", "signature", "verify_key")}
            if core.get("prev_hash") != prev:
                return {"valid": False, "count": len(records), "broken_at": i,
                        "signed": any_signed,
                        "reason": "prev_hash mismatch (record removed or reordered)"}
            expected = hashlib.sha256((_canonical(core) + prev).encode()).hexdigest()
            if expected != rec.get("record_hash"):
                return {"valid": False, "count": len(records), "broken_at": i,
                        "signed": any_signed,
                        "reason": "record_hash mismatch (tampered content)"}
            sig, vk = rec.get("signature"), rec.get("verify_key")
            if require_signatures and (not sig or not vk):
                return {"valid": False, "count": len(records), "broken_at": i,
                        "signed": any_signed,
                        "reason": "missing signature (required) — chain not cryptographically bound"}
            if pin is not None and vk and vk != pin:
                return {"valid": False, "count": len(records), "broken_at": i,
                        "signed": any_signed,
                        "reason": "verify_key does not match pinned identity (possible re-sign attack)"}
            if sig and vk:
                if not _HAVE_NACL:
                    return {"valid": False, "count": len(records), "broken_at": i,
                            "signed": any_signed,
                            "reason": "signed record present but PyNaCl unavailable to verify"}
                try:
                    VerifyKey(vk, encoder=HexEncoder).verify(
                        rec["record_hash"].encode(), bytes.fromhex(sig))
                    any_signed = True
                except Exception:
                    return {"valid": False, "count": len(records), "broken_at": i,
                            "signed": any_signed,
                            "reason": "signature verification failed"}
            prev = rec["record_hash"]

        if any_signed:
            reason = "ok"
        elif _HAVE_NACL:
            reason = "ok (WARNING: no signatures present — not cryptographically tamper-evident)"
        else:
            reason = "ok (hash-chain only; PyNaCl unavailable — not cryptographically tamper-evident)"
        return {"valid": True, "count": len(records), "broken_at": None,
                "signed": any_signed, "reason": reason}
