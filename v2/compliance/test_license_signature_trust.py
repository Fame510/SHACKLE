#!/usr/bin/env python3
"""Regression tests for license signature trust (fail-closed) and issuer custody.

Covers the vulnerability fixed in PR #14 and the startup wiring / stable issuer
key work that followed:

  1. A forged license (attacker's own keypair) must be REJECTED.
  2. An unconfigured server must fail CLOSED, not open.
  3. A genuinely issued license must still be ACCEPTED end to end.
  4. The keygen must refuse to mint an ephemeral issuer key silently.

Run:  pytest v2/compliance/test_license_signature_trust.py -v
"""
import base64
import importlib
import json
import os
import sys
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import license_keygen  # noqa: E402


def _fresh_server(tmp_path, env):
    """Import license_server with a clean DB and a controlled environment."""
    for key in [
        "SHACKLE_MASTER_SECRET",
        "MASTER_SECRET",
        "SHACKLE_LICENSE_PUBKEYS",
        "SHACKLE_LICENSE_PRIVATE_KEY",
        "SHACKLE_LICENSE_SIGNING_KEY_ID",
    ]:
        os.environ.pop(key, None)
    os.environ.update(env)
    os.environ["DATABASE_PATH"] = str(tmp_path / f"licenses-{uuid.uuid4().hex}.db")

    if "license_server" in sys.modules:
        del sys.modules["license_server"]
    return importlib.import_module("license_server")


def _issuer():
    """A stable issuer identity, as bootstrap_issuer would produce."""
    gen = license_keygen.LicenseGenerator(allow_generate=True)
    return {
        "master_secret": gen.export_master_secret(),
        "private_key": gen.export_private_key_b64(),
        "public_key": gen.export_public_key_b64(),
        "key_id": gen.key_id,
        "generator": gen,
    }


def test_forged_license_is_rejected(tmp_path):
    """An attacker who never held the issuer key cannot mint a valid license."""
    iss = _issuer()
    srv = _fresh_server(tmp_path, {
        "SHACKLE_MASTER_SECRET": iss["master_secret"],
        "SHACKLE_LICENSE_PUBKEYS": json.dumps({iss["key_id"]: iss["public_key"]}),
    })
    srv.load_trust_from_env()

    attacker = ed25519.Ed25519PrivateKey.generate()
    license_key = "SHACKLE-ENT-forged-0000000000000000"
    metadata = {"tier": "ENTERPRISE", "seats": 999999, "expires": "2099-12-31"}
    payload = f"{license_key}:{json.dumps(metadata, sort_keys=True)}"
    forged = base64.b64encode(attacker.sign(payload.encode())).decode()

    # Against the trusted registry, with an unknown key_id, and with no key_id.
    assert srv.verify_signature(license_key, metadata, forged, iss["key_id"]) is False
    assert srv.verify_signature(license_key, metadata, forged, "attacker-key") is False
    assert srv.verify_signature(license_key, metadata, forged, None) is False


def test_unconfigured_server_fails_closed(tmp_path):
    """No trust anchors configured means trust nothing, not trust everything."""
    srv = _fresh_server(tmp_path, {})
    srv.load_trust_from_env()

    assert srv.TRUSTED_PUBLIC_KEYS == {}
    assert srv.licensing_status()["licensing_ready"] is False

    signer = ed25519.Ed25519PrivateKey.generate()
    metadata = {"tier": "ENTERPRISE"}
    license_key = "SHACKLE-ENT-x-y"
    payload = f"{license_key}:{json.dumps(metadata, sort_keys=True)}"
    sig = base64.b64encode(signer.sign(payload.encode())).decode()

    assert srv.verify_signature(license_key, metadata, sig, None) is False
    assert srv.verify_signature(license_key, metadata, sig, "default") is False


def test_startup_loads_trust_and_reports_ready(tmp_path):
    """The startup handler must populate trust anchors before serving traffic."""
    iss = _issuer()
    srv = _fresh_server(tmp_path, {
        "SHACKLE_MASTER_SECRET": iss["master_secret"],
        "SHACKLE_LICENSE_PUBKEYS": json.dumps({iss["key_id"]: iss["public_key"]}),
        "SHACKLE_LICENSE_PRIVATE_KEY": iss["private_key"],
        "SHACKLE_LICENSE_SIGNING_KEY_ID": iss["key_id"],
    })

    # Trust is empty until startup runs.
    assert srv.TRUSTED_PUBLIC_KEYS == {}

    from fastapi.testclient import TestClient
    with TestClient(srv.app) as client:
        health = client.get("/health").json()
        assert health["licensing_ready"] is True
        assert health["trust_anchors"] == 1
        assert health["signing_key_configured"] is True
        # No key material may leak through the health endpoint.
        blob = json.dumps(health)
        assert iss["private_key"] not in blob
        assert iss["public_key"] not in blob
        assert iss["master_secret"] not in blob


def test_genuine_license_round_trip(tmp_path):
    """Issue -> register -> validate must succeed, while a forgery is refused."""
    iss = _issuer()
    srv = _fresh_server(tmp_path, {
        "SHACKLE_MASTER_SECRET": iss["master_secret"],
        "SHACKLE_LICENSE_PUBKEYS": json.dumps({iss["key_id"]: iss["public_key"]}),
        "SHACKLE_LICENSE_PRIVATE_KEY": iss["private_key"],
        "SHACKLE_LICENSE_SIGNING_KEY_ID": iss["key_id"],
    })

    # Issue with the SAME stable issuer identity the server trusts.
    gen = license_keygen.LicenseGenerator(
        master_secret=iss["master_secret"],
        private_key_b64=iss["private_key"],
        key_id=iss["key_id"],
    )
    lic = gen.generate_license(customer_name="Corgi Cafe", tier="ENTERPRISE", max_nodes=5)
    assert lic["key_id"] == iss["key_id"]

    from fastapi.testclient import TestClient
    with TestClient(srv.app) as client:
        reg = client.post("/api/v1/licenses/register", json={
            "license_key": lic["license_key"],
            "metadata": lic["metadata"],
            "signature": lic["signature"],
            "key_id": lic["key_id"],
        })
        assert reg.status_code == 200, reg.text

        val = client.post("/api/v1/licenses/validate", json={
            "license_key": lic["license_key"],
            "node_id": "node-1",
        })
        assert val.status_code == 200, val.text
        assert val.json()["valid"] is True

        # Same server, forged signature over an attacker-chosen payload.
        attacker = ed25519.Ed25519PrivateKey.generate()
        forged_meta = dict(lic["metadata"], tier="UNLIMITED", max_nodes=None)
        forged_key = "SHACKLE-ENT-" + str(uuid.uuid4()) + "-deadbeefdeadbeef"
        payload = f"{forged_key}:{json.dumps(forged_meta, sort_keys=True)}"
        forged_sig = base64.b64encode(attacker.sign(payload.encode())).decode()

        bad = client.post("/api/v1/licenses/register", json={
            "license_key": forged_key,
            "metadata": forged_meta,
            "signature": forged_sig,
            "key_id": iss["key_id"],
            "claimed_public_key": base64.b64encode(
                attacker.public_key().public_bytes_raw()
            ).decode(),
        })
        assert bad.status_code == 400, (
            f"forged license was accepted with {bad.status_code}: {bad.text}"
        )


def test_uuid_license_key_parses(tmp_path):
    """Regression: license_id is a UUID, so the key has more than 4 segments.

    The generator emits SHACKLE-ENT-<uuid>-<checksum>. A 4-part split rejected
    every real key as malformed, which blocked registration of genuine licenses
    before signature verification was ever reached.
    """
    srv = _fresh_server(tmp_path, {})
    key = "SHACKLE-ENT-22df7ea0-73b1-405b-9c79-833c2d5f737c-91692f4515501b19"

    parsed = srv.parse_license_key(key)
    assert parsed is not None, "UUID-bearing license key must parse"
    assert parsed["license_id"] == "22df7ea0-73b1-405b-9c79-833c2d5f737c"
    assert parsed["checksum"] == "91692f4515501b19"

    # Malformed keys must still be rejected.
    assert srv.parse_license_key("SHACKLE-ENT-onlyone") is None
    assert srv.parse_license_key("NOTSHACKLE-ENT-a-b") is None
    assert srv.parse_license_key("SHACKLE-PRO-a-b") is None


def test_checksum_valid_forgery_dies_at_signature_gate(tmp_path):
    """Defense in depth: a valid checksum must not buy a forged signature.

    Models an attacker who obtained the HMAC master secret but NOT the issuer
    private key. The checksum check passes, so this exercises the signature
    trust gate itself rather than the cheaper format/checksum checks.
    """
    import hashlib
    import hmac as _hmac

    iss = _issuer()
    srv = _fresh_server(tmp_path, {
        "SHACKLE_MASTER_SECRET": iss["master_secret"],
        "SHACKLE_LICENSE_PUBKEYS": json.dumps({iss["key_id"]: iss["public_key"]}),
    })

    lid = str(uuid.uuid4())
    metadata = {
        "customer": "Attacker Inc", "tier": "UNLIMITED", "max_nodes": None,
        "features": ["proxy"], "issued_at": "2026-09-13T00:00:00",
        "expires_at": "2099-12-31T00:00:00", "node_binding": None,
    }
    checksum = _hmac.new(
        iss["master_secret"].encode(),
        f"{lid}:{json.dumps(metadata, sort_keys=True)}".encode(),
        hashlib.sha256,
    ).hexdigest()[:16]
    license_key = f"SHACKLE-ENT-{lid}-{checksum}"
    payload = f"{license_key}:{json.dumps(metadata, sort_keys=True)}"

    attacker = ed25519.Ed25519PrivateKey.generate()
    forged_sig = base64.b64encode(attacker.sign(payload.encode())).decode()

    from fastapi.testclient import TestClient
    with TestClient(srv.app) as client:
        # Checksum is valid, so this must fail specifically on the signature.
        bad = client.post("/api/v1/licenses/register", json={
            "license_key": license_key, "metadata": metadata,
            "signature": forged_sig, "key_id": iss["key_id"],
        })
        assert bad.status_code == 400, bad.text
        assert "signature" in bad.text.lower(), bad.text

        # Control: the identical payload signed by the real issuer is accepted,
        # which proves the rejection is about WHO signed, not the payload.
        real = ed25519.Ed25519PrivateKey.from_private_bytes(
            base64.b64decode(iss["private_key"])
        )
        good_sig = base64.b64encode(real.sign(payload.encode())).decode()
        ok = client.post("/api/v1/licenses/register", json={
            "license_key": license_key, "metadata": metadata,
            "signature": good_sig, "key_id": iss["key_id"],
        })
        assert ok.status_code == 200, ok.text


def test_keygen_refuses_ephemeral_issuer_key():
    """Silently minting a per-run key produces licenses nobody can validate."""
    for key in ["SHACKLE_LICENSE_PRIVATE_KEY", "SHACKLE_LICENSE_SIGNING_KEY_ID"]:
        os.environ.pop(key, None)

    with pytest.raises(ValueError, match="issuer signing key"):
        license_keygen.LicenseGenerator(master_secret="test-secret")


def test_keygen_requires_master_secret():
    for key in ["SHACKLE_MASTER_SECRET", "MASTER_SECRET"]:
        os.environ.pop(key, None)

    with pytest.raises(ValueError, match="master secret"):
        license_keygen.LicenseGenerator()


def test_issuer_key_is_stable_across_runs():
    """Two generators loading the same seed must produce the same public key."""
    iss = _issuer()
    a = license_keygen.LicenseGenerator(
        master_secret=iss["master_secret"],
        private_key_b64=iss["private_key"],
        key_id=iss["key_id"],
    )
    b = license_keygen.LicenseGenerator(
        master_secret=iss["master_secret"],
        private_key_b64=iss["private_key"],
        key_id=iss["key_id"],
    )
    assert a.export_public_key_b64() == b.export_public_key_b64() == iss["public_key"]
    assert a.generated_new_signing_key is False
