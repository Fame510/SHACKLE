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
