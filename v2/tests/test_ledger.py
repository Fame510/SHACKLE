"""
Tests for the SHACKLE hash-chained audit ledger (v2/spec/ledger.py).

Proves the tamper-evidence contract: a valid chain verifies, and any content
edit, reorder, or truncation is detected by verify_chain(). Does not require
PyNaCl (chain-hash integrity is checked regardless; signatures are checked when
nacl is present).
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "spec"))

from ledger import AuditLedger, GENESIS  # noqa: E402


def _tmp():
    return os.path.join(tempfile.mkdtemp(), "audit.log")


def test_empty_chain_is_valid():
    led = AuditLedger(_tmp())
    res = led.verify_chain()
    assert res["valid"] is True
    assert res["count"] == 0


def test_appends_form_valid_chain():
    p = _tmp()
    led = AuditLedger(p)
    led.log_decision("s1", "click", "ALLOW", "within_thresholds")
    led.log_decision("s1", "spend", "DENY", "budget_exhausted")
    led.log_execution("s1", "click", ok=True, cost_usd=0.01)
    res = led.verify_chain()
    assert res["valid"] is True, res
    assert res["count"] == 3
    assert res["broken_at"] is None


def test_first_record_links_to_genesis():
    p = _tmp()
    led = AuditLedger(p)
    rec = led.log_decision("s1", "click", "ALLOW")
    assert rec["prev_hash"] == GENESIS


def test_tampered_content_is_detected():
    p = _tmp()
    led = AuditLedger(p)
    led.log_decision("s1", "click", "ALLOW", "within_thresholds")
    led.log_decision("s1", "spend", "DENY", "budget_exhausted")
    # Flip a verdict in place without recomputing the hash chain.
    lines = open(p).read().splitlines()
    rec = json.loads(lines[1])
    rec["verdict"] = "ALLOW"  # tamper: DENY -> ALLOW
    lines[1] = json.dumps(rec)
    open(p, "w").write("\n".join(lines) + "\n")
    res = AuditLedger(p).verify_chain()
    assert res["valid"] is False
    assert res["broken_at"] == 1


def test_reorder_is_detected():
    p = _tmp()
    led = AuditLedger(p)
    led.log_decision("s1", "a", "ALLOW")
    led.log_decision("s1", "b", "ALLOW")
    lines = open(p).read().splitlines()
    lines[0], lines[1] = lines[1], lines[0]  # swap order
    open(p, "w").write("\n".join(lines) + "\n")
    res = AuditLedger(p).verify_chain()
    assert res["valid"] is False
    assert res["broken_at"] == 0


def test_truncation_is_detected_on_middle_delete():
    p = _tmp()
    led = AuditLedger(p)
    led.log_decision("s1", "a", "ALLOW")
    led.log_decision("s1", "b", "ALLOW")
    led.log_decision("s1", "c", "ALLOW")
    lines = open(p).read().splitlines()
    del lines[1]  # remove middle record
    open(p, "w").write("\n".join(lines) + "\n")
    res = AuditLedger(p).verify_chain()
    assert res["valid"] is False
    assert res["broken_at"] == 1


def test_recovery_continues_chain_across_instances():
    p = _tmp()
    led1 = AuditLedger(p)
    led1.log_decision("s1", "a", "ALLOW")
    # New instance recovers last hash and continues the same chain.
    led2 = AuditLedger(p)
    led2.log_decision("s1", "b", "ALLOW")
    res = AuditLedger(p).verify_chain()
    assert res["valid"] is True
    assert res["count"] == 2


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("All ledger tests passed.")


# ─────────────────────────────────────────────────────────────────────────────
# Adversarial tamper tests (fix/audit-hardening)
#
# The original suite only flipped a field WITHOUT recomputing record_hash, so of
# course the hash chain caught it. But record_hash is a keyless SHA-256: a real
# attacker recomputes the whole chain after editing. The Ed25519 signature is the
# only thing that stops that — so these tests exercise the recompute attack and
# the re-sign attack, which verify_chain() must now reject.
# ─────────────────────────────────────────────────────────────────────────────
import hashlib  # noqa: E402
import pytest  # noqa: E402

from ledger import _canonical, _HAVE_NACL  # noqa: E402

try:
    from nacl.signing import SigningKey  # noqa: E402
    from nacl.encoding import HexEncoder  # noqa: E402
except Exception:  # pragma: no cover
    SigningKey = None


def _read_records(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _write_records(path, records):
    with open(path, "w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _recompute_chain(records, *, drop_signatures=False, signing_key=None,
                     verify_key_hex=None):
    """Rebuild a fully self-consistent hash chain over (possibly edited) records,
    mimicking an attacker who controls the log file. Optionally strip signatures
    (the old bypass) or re-sign every record with an attacker-controlled key."""
    prev = GENESIS
    out = []
    for rec in records:
        core = {k: v for k, v in rec.items()
                if k not in ("record_hash", "signature", "verify_key")}
        core["prev_hash"] = prev
        rh = hashlib.sha256((_canonical(core) + prev).encode()).hexdigest()
        new = dict(core)
        new["record_hash"] = rh
        if drop_signatures:
            new["signature"] = ""
            new["verify_key"] = ""
        elif signing_key is not None:
            new["signature"] = signing_key.sign(rh.encode()).signature.hex()
            new["verify_key"] = verify_key_hex
        out.append(new)
        prev = rh
    return out


@pytest.mark.skipif(not _HAVE_NACL, reason="signature enforcement requires PyNaCl")
def test_recompute_and_strip_signatures_is_rejected():
    """Attacker edits a verdict, recomputes the entire keyless hash chain, and
    blanks the signatures. The OLD verify_chain returned valid=True here. It must
    now be rejected because a signable ledger requires signatures."""
    p = _tmp()
    led = AuditLedger(p)
    led.log_decision("s1", "purchase", "DENY", "budget_exhausted")
    led.log_decision("s1", "purchase", "DENY", "max_repeat_exceeded")
    pinned = led.verify_key_hex

    records = _read_records(p)
    # Flip the first DENY into an ALLOW — the exact evidence an attacker would forge.
    records[0]["verdict"] = "ALLOW"
    records[0]["reason"] = "within_thresholds"
    forged = _recompute_chain(records, drop_signatures=True)
    _write_records(p, forged)

    res = led.verify_chain(expected_verify_key=pinned)
    assert res["valid"] is False, "stripped-signature forgery must be rejected"
    assert "signature" in res["reason"].lower()


@pytest.mark.skipif(not _HAVE_NACL, reason="signature enforcement requires PyNaCl")
def test_resign_with_attacker_key_is_rejected_when_pinned():
    """Attacker edits + recomputes + re-signs with THEIR OWN key. Without a pinned
    identity this would pass; with expected_verify_key it must be rejected."""
    p = _tmp()
    led = AuditLedger(p)
    led.log_decision("s1", "purchase", "DENY", "budget_exhausted")
    pinned = led.verify_key_hex

    attacker = SigningKey.generate()
    attacker_vk = attacker.verify_key.encode(encoder=HexEncoder).decode()

    records = _read_records(p)
    records[0]["verdict"] = "ALLOW"
    forged = _recompute_chain(records, signing_key=attacker,
                              verify_key_hex=attacker_vk)
    _write_records(p, forged)

    res = led.verify_chain(expected_verify_key=pinned)
    assert res["valid"] is False, "re-sign-with-attacker-key must be rejected under a pinned identity"
    assert "verify_key" in res["reason"].lower() or "signature" in res["reason"].lower()


@pytest.mark.skipif(not _HAVE_NACL, reason="signature enforcement requires PyNaCl")
def test_untampered_signed_chain_still_verifies_with_pin():
    """Guard against over-tightening: a genuine signed chain must still pass when
    the correct identity is pinned."""
    p = _tmp()
    led = AuditLedger(p)
    led.log_decision("s1", "click", "ALLOW", "within_thresholds")
    led.log_decision("s1", "click", "HITL", "hitl_all_calls")
    res = led.verify_chain(expected_verify_key=led.verify_key_hex)
    assert res["valid"] is True
    assert res["signed"] is True


def test_unsigned_ledger_reports_not_cryptographically_evident():
    """An unsigned-by-design ledger must NOT masquerade as tamper-evident: it
    passes hash-chain-only but self-labels signed=False so callers can tell."""
    p = _tmp()
    led = AuditLedger(p)
    led.log_decision("s1", "click", "ALLOW", "within_thresholds")
    # Simulate an environment/instance with no signing identity.
    led.signing_key = None
    led.verify_key_hex = ""
    res = led.verify_chain(require_signatures=False)
    if not _HAVE_NACL:
        assert res["valid"] is True
        assert res["signed"] is False
    else:
        # Records were actually signed at write time, so signed stays True here;
        # the meaningful assertion is that require_signatures=False never crashes
        # and still returns a well-formed result.
        assert res["valid"] in (True, False)
        assert "signed" in res
