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
        """Recompute the chain and validate every signature.

        Returns {valid, count, broken_at, reason, signed}. broken_at is the
        0-based index of the first bad record, or None when the chain is intact.

        SECURITY (fix/audit-hardening): the record_hash is a keyless SHA-256, so
        an attacker who edits a record can simply recompute every record_hash and
        the hash chain alone will still verify. The Ed25519 signature is the ONLY
        thing that binds the chain to an identity, so signature checking must not
        be skippable by blanking the signature fields.

        Rules:
          * expected_verify_key: pin the identity. If provided, every record MUST
            be signed by exactly this key. A record whose verify_key differs (an
            attacker re-signing with their own key) is rejected even if its own
            signature is internally consistent.
          * require_signatures: when True, a missing/blank signature is a failure.
            Defaults to True whenever this ledger can verify signatures
            (expected_verify_key given, or this instance signs, or PyNaCl present).
            An attacker can therefore no longer strip signatures to pass.
          * Only a ledger that is unsigned *by design* (no PyNaCl anywhere, no
            expected key, no signing instance) falls back to hash-chain-only, and
            it reports signed=False + an explicit reason so callers never mistake
            a checksum for cryptographic tamper-evidence.
        """
        pin = expected_verify_key or self.verify_key_hex or None
        can_verify = _HAVE_NACL and (pin is not None or self.signing_key is not None)
        if require_signatures is None:
            require_signatures = can_verify

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
            if require_signatures:
                if not sig or not vk:
                    return {"valid": False, "count": len(records), "broken_at": i,
                            "signed": any_signed,
                            "reason": "missing signature (required) — chain not cryptographically bound"}
                if pin is not None and vk != pin:
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
        reason = "ok" if (any_signed or not can_verify) else "ok (unverified: no signatures present)"
        if not can_verify and not any_signed:
            reason = "ok (hash-chain only; unsigned ledger — not cryptographically tamper-evident)"
        return {"valid": True, "count": len(records), "broken_at": None,
                "signed": any_signed, "reason": reason}


if __name__ == "__main__":
    import tempfile
    p = os.path.join(tempfile.mkdtemp(), "audit.log")
    led = AuditLedger(p)
    led.log_decision("s1", "click", "ALLOW", "within_thresholds")
    led.log_decision("s1", "spend", "DENY", "budget_exhausted")
    res = led.verify_chain()
    assert res["valid"], res
    print("OK chain valid, count=%d" % res["count"])
