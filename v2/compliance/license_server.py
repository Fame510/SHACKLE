#!/usr/bin/env python3
"""
SHACKLE-V2 License Validation Server
FastAPI server with SQLite database for license validation and audit logging
"""

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import sqlite3
import hashlib
import hmac
import json
import base64
from datetime import datetime, timedelta
from contextlib import contextmanager
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization
import logging
import os
import secrets
import uvicorn

logger = logging.getLogger("shackle.license_server")

# Configuration
# DATABASE_PATH comes from the environment so container / systemd deploys can
# point it at a mounted volume (e.g. /data/licenses.db) without code edits.
DATABASE_PATH = os.environ.get("DATABASE_PATH", "licenses.db")
MASTER_SECRET = None  # Set via environment or init
PUBLIC_KEY = None  # DEPRECATED single-key slot; kept for back-compat only.
# key_id -> base64 Ed25519 public key. Server-side trust anchor(s). A license is
# only trusted if signed by a key present here. Populated at init from a
# server-controlled source (env/secret store), NEVER from the request.
TRUSTED_PUBLIC_KEYS: Dict[str, str] = {}
# Optional server-side signing key (base64 Ed25519 private seed) used by the
# license issuer/helper. Loaded from a secret store; never committed, never
# returned in any response.
SIGNING_PRIVATE_KEY = None
SIGNING_KEY_ID = None

app = FastAPI(
    title="SHACKLE-V2 License Server",
    description="Enterprise license validation and audit API",
    version="2.0.0"
)

security = HTTPBearer()


# Pydantic models
class LicenseValidationRequest(BaseModel):
    license_key: str
    node_id: Optional[str] = None
    hardware_id: Optional[str] = None


class LicenseValidationResponse(BaseModel):
    valid: bool
    license_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    errors: List[str] = []
    remaining_days: Optional[int] = None


class AuditLogEntry(BaseModel):
    timestamp: str
    event_type: str
    license_key: str
    node_id: Optional[str] = None
    result: str
    signature: str


class LicenseRegistration(BaseModel):
    license_key: str
    metadata: Dict[str, Any]
    signature: str
    # key_id identifies WHICH server-trusted public key signed this license.
    # Verification uses TRUSTED_PUBLIC_KEYS[key_id], never a client-supplied key.
    key_id: Optional[str] = None
    # Audit-only record of any public key the client claimed. SECURITY: this is
    # NEVER used for verification. Retained only for forensic/audit purposes.
    claimed_public_key: Optional[str] = None


# Database management
@contextmanager
def get_db():
    """Context manager for database connections"""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def configure_trust(master_secret: str,
                    trusted_public_keys: Optional[Dict[str, str]] = None,
                    signing_private_key: Optional[str] = None,
                    signing_key_id: Optional[str] = None) -> None:
    """Configure server-side secrets/trust anchors from a controlled source.

    Call once at startup with values loaded from environment / secret store.
    NEVER hardcode these in the repo and NEVER accept them from a request.
      master_secret        : HMAC secret for verify_checksum.
      trusted_public_keys  : {key_id: base64 Ed25519 public key} the server trusts.
      signing_private_key  : base64 Ed25519 private seed for issuing licenses.
      signing_key_id       : key_id that pairs with signing_private_key.
    """
    global MASTER_SECRET, TRUSTED_PUBLIC_KEYS, SIGNING_PRIVATE_KEY, SIGNING_KEY_ID
    # Only overwrite what the caller actually supplied. A later call (e.g. the
    # startup env load) must not wipe configuration an earlier caller set, which
    # would silently return the server to a fail-closed state that rejects every
    # legitimate license.
    if master_secret is not None:
        MASTER_SECRET = master_secret
    if trusted_public_keys:
        TRUSTED_PUBLIC_KEYS = dict(trusted_public_keys)
    if signing_private_key is not None:
        SIGNING_PRIVATE_KEY = signing_private_key
    if signing_key_id is not None:
        SIGNING_KEY_ID = signing_key_id


def load_trust_from_env() -> None:
    """Load secrets from environment variables (safe for phone/dashboard deploys).

    Env vars (set via your hosting platform's secret manager, not the repo):
      SHACKLE_MASTER_SECRET          : HMAC secret.
      SHACKLE_LICENSE_PUBKEYS        : JSON object {key_id: base64_pubkey}, OR a
                                       single base64 pubkey (mapped to key_id 'default').
      SHACKLE_LICENSE_PRIVATE_KEY    : base64 Ed25519 private seed (issuer only).
      SHACKLE_LICENSE_SIGNING_KEY_ID : key_id paired with the private key.
    Fails closed: if pubkeys are absent, TRUSTED_PUBLIC_KEYS stays empty and all
    signature verification returns False.

    MASTER_SECRET is also accepted as a legacy alias of SHACKLE_MASTER_SECRET so
    existing docker-compose / systemd EnvironmentFile deploys keep working.
    """
    master = (
        os.environ.get("SHACKLE_MASTER_SECRET")
        or os.environ.get("MASTER_SECRET")
    )
    pubkeys_raw = os.environ.get("SHACKLE_LICENSE_PUBKEYS")
    trusted: Dict[str, str] = {}
    if pubkeys_raw:
        try:
            parsed = json.loads(pubkeys_raw)
            if isinstance(parsed, dict):
                trusted = {str(k): str(v) for k, v in parsed.items()}
            else:
                trusted = {"default": str(parsed)}
        except (ValueError, TypeError):
            # Treat as a single bare base64 key.
            trusted = {"default": pubkeys_raw}
    configure_trust(
        master_secret=master,
        trusted_public_keys=trusted,
        signing_private_key=os.environ.get("SHACKLE_LICENSE_PRIVATE_KEY"),
        signing_key_id=os.environ.get("SHACKLE_LICENSE_SIGNING_KEY_ID"),
    )


def licensing_status() -> Dict[str, Any]:
    """Report licensing readiness WITHOUT exposing any key material.

    Counts and booleans only: never a secret, never a public key, never a key_id
    value that could aid an attacker in probing the trust registry.
    """
    return {
        "master_secret_configured": bool(MASTER_SECRET),
        "trust_anchors": len(TRUSTED_PUBLIC_KEYS),
        "signing_key_configured": bool(SIGNING_PRIVATE_KEY and SIGNING_KEY_ID),
        # Ready to accept genuine licenses: needs the HMAC secret for checksum
        # verification AND at least one trust anchor for signature verification.
        "licensing_ready": bool(MASTER_SECRET) and len(TRUSTED_PUBLIC_KEYS) > 0,
    }


def sign_license(license_key: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Issuer helper: sign a license with the server-held private key.

    Returns {signature, key_id}. Raises if no signing key is configured. This is
    how the operator mints licenses; customers never sign their own.
    """
    if not SIGNING_PRIVATE_KEY or not SIGNING_KEY_ID:
        raise ValueError("No signing key configured (set SHACKLE_LICENSE_PRIVATE_KEY / _SIGNING_KEY_ID)")
    payload = f"{license_key}:{json.dumps(metadata, sort_keys=True)}"
    seed = base64.b64decode(SIGNING_PRIVATE_KEY)
    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
    signature = private_key.sign(payload.encode())
    return {"signature": base64.b64encode(signature).decode(), "key_id": SIGNING_KEY_ID}


def init_database():
    """Initialize database schema"""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS licenses (
                license_id TEXT PRIMARY KEY,
                license_key TEXT UNIQUE NOT NULL,
                customer TEXT NOT NULL,
                tier TEXT NOT NULL,
                max_nodes INTEGER,
                features TEXT NOT NULL,
                issued_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                node_binding TEXT,
                metadata TEXT NOT NULL,
                signature TEXT NOT NULL,
                public_key TEXT NOT NULL,
                activated_at TEXT,
                last_validated TEXT,
                status TEXT DEFAULT 'active'
            );
            
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                license_key TEXT NOT NULL,
                license_id TEXT,
                node_id TEXT,
                hardware_id TEXT,
                ip_address TEXT,
                result TEXT NOT NULL,
                error_message TEXT,
                signature TEXT NOT NULL,
                metadata TEXT
            );
            
            CREATE TABLE IF NOT EXISTS api_keys (
                key_id TEXT PRIMARY KEY,
                key_hash TEXT UNIQUE NOT NULL,
                description TEXT,
                created_at TEXT NOT NULL,
                last_used TEXT,
                permissions TEXT NOT NULL
            );
            
            CREATE INDEX IF NOT EXISTS idx_audit_license ON audit_log(license_key);
            CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
            CREATE INDEX IF NOT EXISTS idx_licenses_status ON licenses(status);
        """)


def parse_license_key(license_key: str) -> Optional[Dict[str, str]]:
    """Parse SHACKLE license key format"""
    parts = license_key.split("-")
    # Format: SHACKLE-ENT-<license_id>-<checksum>, where license_id is a UUID.
    # A UUID contains hyphens of its own, so a naive 4-part split rejects every
    # key the generator actually produces. Anchor on the known prefix and the
    # trailing checksum, and rejoin everything between them as the license_id.
    if len(parts) < 4 or parts[0] != "SHACKLE" or parts[1] != "ENT":
        return None

    license_id = "-".join(parts[2:-1])
    checksum = parts[-1]
    if not license_id or not checksum:
        return None
    
    return {
        "prefix": f"{parts[0]}-{parts[1]}",
        "license_id": license_id,
        "checksum": checksum
    }


def verify_checksum(license_key: str, metadata: Dict[str, Any]) -> bool:
    """Verify license key checksum"""
    if not MASTER_SECRET:
        raise ValueError("Master secret not configured")
    
    parsed = parse_license_key(license_key)
    if not parsed:
        return False
    
    # Reconstruct checksum
    checksum_input = f"{parsed['license_id']}:{json.dumps(metadata, sort_keys=True)}"
    expected_checksum = hmac.new(
        MASTER_SECRET.encode(),
        checksum_input.encode(),
        hashlib.sha256
    ).hexdigest()[:16]
    
    return hmac.compare_digest(expected_checksum, parsed['checksum'])


def verify_signature(license_key: str, metadata: Dict[str, Any], signature: str, key_id: Optional[str] = None) -> bool:
    """Verify a license Ed25519 signature against a SERVER-SIDE trust anchor.

    SECURITY: the signing public key is resolved from TRUSTED_PUBLIC_KEYS (a
    server-controlled registry), NOT from anything supplied in the request. A
    client-supplied key would let anyone forge a 'valid' signature over their
    own payload. Fails CLOSED: if no trusted key is configured, or key_id is
    unknown, verification returns False.
    """
    try:
        payload = f"{license_key}:{json.dumps(metadata, sort_keys=True)}"
        signature_bytes = base64.b64decode(signature)

        # Resolve trusted key(s). If key_id given, use exactly that key; else try
        # every configured trusted key (supports rotation / multiple issuers).
        candidates = []
        if key_id is not None:
            pk = TRUSTED_PUBLIC_KEYS.get(key_id)
            if pk is not None:
                candidates.append(pk)
        else:
            candidates = list(TRUSTED_PUBLIC_KEYS.values())

        # Fail closed: no trusted key configured => trust nothing.
        if not candidates:
            return False

        for pk_b64 in candidates:
            try:
                public_key_bytes = base64.b64decode(pk_b64)
                public_key = ed25519.Ed25519PublicKey.from_public_bytes(public_key_bytes)
                public_key.verify(signature_bytes, payload.encode())
                return True
            except Exception:
                continue
        return False
    except Exception:
        return False

def sign_audit_entry(event_data: Dict[str, Any]) -> str:
    """Sign audit log entry with server key"""
    # Use HMAC for audit signatures (server-side only)
    payload = json.dumps(event_data, sort_keys=True)
    signature = hmac.new(
        MASTER_SECRET.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()
    return signature


def log_audit_event(
    event_type: str,
    license_key: str,
    result: str,
    license_id: Optional[str] = None,
    node_id: Optional[str] = None,
    hardware_id: Optional[str] = None,
    error_message: Optional[str] = None,
    ip_address: Optional[str] = None,
    metadata: Optional[Dict] = None
):
    """Log audit event with non-repudiation signature"""
    timestamp = datetime.utcnow().isoformat()
    
    event_data = {
        "timestamp": timestamp,
        "event_type": event_type,
        "license_key": license_key,
        "result": result
    }
    
    signature = sign_audit_entry(event_data)
    
    with get_db() as conn:
        conn.execute("""
            INSERT INTO audit_log (
                timestamp, event_type, license_key, license_id, node_id,
                hardware_id, ip_address, result, error_message, signature, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            timestamp, event_type, license_key, license_id, node_id,
            hardware_id, ip_address, result, error_message, signature,
            json.dumps(metadata) if metadata else None
        ))


# API endpoints
@app.on_event("startup")
async def startup():
    """Initialize on startup.

    Trust anchors are loaded here, before the server accepts traffic. Without
    this call TRUSTED_PUBLIC_KEYS stays empty and, because verification fails
    closed, every legitimate license is rejected. Startup is the only correct
    place for it: request handlers must never populate trust from request data.
    """
    init_database()
    load_trust_from_env()

    status = licensing_status()
    if status["licensing_ready"]:
        logger.info(
            "License server ready: %d trust anchor(s) loaded, issuer signing key %s.",
            status["trust_anchors"],
            "configured" if status["signing_key_configured"] else "not configured",
        )
    else:
        # Loud, actionable, and safe to log: names the missing variables without
        # printing any value.
        missing = []
        if not status["master_secret_configured"]:
            missing.append("SHACKLE_MASTER_SECRET")
        if status["trust_anchors"] == 0:
            missing.append("SHACKLE_LICENSE_PUBKEYS")
        logger.error(
            "License server is FAIL-CLOSED: %s not set. Every license "
            "verification will be rejected until these are configured in the "
            "deployment environment. See v2/compliance/DEPLOYMENT-LICENSING.md.",
            ", ".join(missing),
        )


@app.post("/api/v1/licenses/register", response_model=Dict[str, str])
async def register_license(registration: LicenseRegistration):
    """
    Register a new license in the database
    Requires valid signature from license generator
    """
    # Parse license key
    parsed = parse_license_key(registration.license_key)
    if not parsed:
        raise HTTPException(status_code=400, detail="Invalid license key format")
    
    # Verify checksum
    if not verify_checksum(registration.license_key, registration.metadata):
        raise HTTPException(status_code=400, detail="Invalid license checksum")
    
    # Verify signature
    if not verify_signature(
        registration.license_key,
        registration.metadata,
        registration.signature,
        registration.key_id
    ):
        raise HTTPException(status_code=400, detail="Invalid license signature")
    
    # Store in database
    try:
        with get_db() as conn:
            conn.execute("""
                INSERT INTO licenses (
                    license_id, license_key, customer, tier, max_nodes,
                    features, issued_at, expires_at, node_binding,
                    metadata, signature, public_key, activated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                parsed['license_id'],
                registration.license_key,
                registration.metadata['customer'],
                registration.metadata['tier'],
                registration.metadata.get('max_nodes'),
                json.dumps(registration.metadata['features']),
                registration.metadata['issued_at'],
                registration.metadata['expires_at'],
                registration.metadata.get('node_binding'),
                json.dumps(registration.metadata),
                registration.signature,
                (registration.claimed_public_key or ''),  # audit-only; NOT used for verification
                datetime.utcnow().isoformat()
            ))
        
        log_audit_event("LICENSE_REGISTERED", registration.license_key, "success", parsed['license_id'])
        
        return {"status": "registered", "license_id": parsed['license_id']}
    
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="License already registered")


@app.post("/api/v1/licenses/validate", response_model=LicenseValidationResponse)
async def validate_license(request: LicenseValidationRequest):
    """
    Validate a license key
    Returns license metadata if valid, errors if invalid
    """
    errors = []
    
    # Parse license key
    parsed = parse_license_key(request.license_key)
    if not parsed:
        log_audit_event("VALIDATION_FAILED", request.license_key, "failure", 
                       error_message="Invalid license format")
        return LicenseValidationResponse(valid=False, errors=["Invalid license key format"])
    
    # Fetch from database
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM licenses WHERE license_key = ?",
            (request.license_key,)
        ).fetchone()
    
    if not row:
        log_audit_event("VALIDATION_FAILED", request.license_key, "failure",
                       error_message="License not found")
        return LicenseValidationResponse(valid=False, errors=["License not registered"])
    
    license_data = dict(row)
    metadata = json.loads(license_data['metadata'])
    
    # Check expiration
    expires_at = datetime.fromisoformat(license_data['expires_at'])
    now = datetime.utcnow()
    
    if now > expires_at:
        errors.append("License expired")
    
    remaining_days = (expires_at - now).days if now <= expires_at else 0
    
    # Check status
    if license_data['status'] != 'active':
        errors.append(f"License status: {license_data['status']}")
    
    # Check node binding if specified
    if license_data['node_binding'] and request.hardware_id:
        if license_data['node_binding'] != request.hardware_id:
            errors.append("Hardware ID mismatch")
    
    # Update last validated
    with get_db() as conn:
        conn.execute(
            "UPDATE licenses SET last_validated = ? WHERE license_key = ?",
            (datetime.utcnow().isoformat(), request.license_key)
        )
    
    valid = len(errors) == 0
    result = "success" if valid else "failure"
    
    log_audit_event(
        "LICENSE_VALIDATED",
        request.license_key,
        result,
        license_id=license_data['license_id'],
        node_id=request.node_id,
        hardware_id=request.hardware_id,
        error_message="; ".join(errors) if errors else None
    )
    
    return LicenseValidationResponse(
        valid=valid,
        license_id=license_data['license_id'],
        metadata=metadata if valid else None,
        errors=errors,
        remaining_days=remaining_days if valid else None
    )


@app.get("/api/v1/audit/export")
async def export_audit_log(
    license_key: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 1000
):
    """
    Export audit logs with signature verification
    Returns JSONL format for compliance teams
    """
    query = "SELECT * FROM audit_log WHERE 1=1"
    params = []
    
    if license_key:
        query += " AND license_key = ?"
        params.append(license_key)
    
    if start_date:
        query += " AND timestamp >= ?"
        params.append(start_date)
    
    if end_date:
        query += " AND timestamp <= ?"
        params.append(end_date)
    
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    
    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()
    
    entries = []
    for row in rows:
        entry = dict(row)
        # Parse metadata JSON
        if entry['metadata']:
            entry['metadata'] = json.loads(entry['metadata'])
        entries.append(entry)
    
    return {
        "total": len(entries),
        "entries": entries,
        "format": "jsonl",
        "exported_at": datetime.utcnow().isoformat()
    }


@app.get("/health")
async def health_check():
    """Health check endpoint.

    Includes licensing readiness so an operator can confirm a deployment is
    actually able to validate licenses instead of silently rejecting all of
    them. Reports counts and booleans only, never key material.
    """
    status = licensing_status()
    return {
        "status": "healthy",
        "service": "shackle-license-server",
        "version": "2.0.0",
        "licensing_ready": status["licensing_ready"],
        "trust_anchors": status["trust_anchors"],
        "master_secret_configured": status["master_secret_configured"],
        "signing_key_configured": status["signing_key_configured"],
    }


def init_config(master_secret: str):
    """Initialize server configuration"""
    global MASTER_SECRET
    MASTER_SECRET = master_secret


if __name__ == "__main__":
    import sys

    # The master secret may be passed as argv[1] (legacy systemd ExecStart) or,
    # preferably, supplied via the environment. It is NOT required here: the
    # startup handler loads trust from the environment, and an unconfigured
    # server still boots and serves /health so the operator can see it is
    # fail-closed rather than watching the container crash-loop with no signal.
    if len(sys.argv) > 1 and sys.argv[1].strip():
        init_config(sys.argv[1])

    print("🚀 Starting SHACKLE-V2 License Server")
    print(f"📊 Database: {DATABASE_PATH}")

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
