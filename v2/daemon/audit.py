#!/usr/bin/env python3
"""
SHACKLE Audit Logger - Postgres append-only logs with Ed25519 signatures
"""

import base64
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import asyncpg
from nacl.signing import SigningKey, VerifyKey
from nacl.encoding import HexEncoder

logger = logging.getLogger(__name__)


def _decode_key_material(raw: str) -> bytes:
    """Decode a 32-byte Ed25519 seed from hex or base64 text."""
    raw = raw.strip()
    # Try hex first (64 hex chars), then base64.
    try:
        material = bytes.fromhex(raw)
        if len(material) == 32:
            return material
    except ValueError:
        pass
    try:
        material = base64.b64decode(raw, validate=True)
        if len(material) == 32:
            return material
    except Exception:
        pass
    raise ValueError(
        "SHACKLE signing key must be a 32-byte Ed25519 seed encoded as hex (64 chars) "
        "or standard base64."
    )


def load_signing_key() -> "tuple[SigningKey, bool]":
    """
    Load the Ed25519 signing key, in priority order:
      1. SHACKLE_SIGNING_KEY env var (hex or base64 32-byte seed)
      2. SHACKLE_SIGNING_KEY_FILE path (file containing the encoded seed)
      3. Generate a fresh ephemeral key (NOT durable across restarts)

    Returns (SigningKey, is_persistent). When is_persistent is False the audit
    trail will not verify after a process restart, so a loud warning is emitted.

    Production note: inject SHACKLE_SIGNING_KEY from your secrets manager
    (AWS Secrets Manager, GCP Secret Manager, Vault, etc.). Those systems expose
    the secret as an environment variable or mounted file, both of which are read
    here without any code change.
    """
    env_key = os.getenv("SHACKLE_SIGNING_KEY")
    if env_key:
        seed = _decode_key_material(env_key)
        logger.info("Loaded audit signing key from SHACKLE_SIGNING_KEY.")
        return SigningKey(seed), True

    key_file = os.getenv("SHACKLE_SIGNING_KEY_FILE")
    if key_file:
        path = Path(key_file)
        if path.is_file():
            seed = _decode_key_material(path.read_text())
            logger.info("Loaded audit signing key from SHACKLE_SIGNING_KEY_FILE=%s", key_file)
            return SigningKey(seed), True
        logger.error(
            "SHACKLE_SIGNING_KEY_FILE=%s does not exist; falling back to an ephemeral key.",
            key_file,
        )

    logger.warning(
        "No SHACKLE_SIGNING_KEY or SHACKLE_SIGNING_KEY_FILE set. Generating an EPHEMERAL "
        "audit signing key: records signed by this process will NOT verify after a restart. "
        "Set SHACKLE_SIGNING_KEY (from a secrets manager) for durable, verifiable audit logs."
    )
    return SigningKey.generate(), False


def _key_id_for(verify_key: VerifyKey) -> str:
    """Stable short identifier for a public key (first 8 bytes, hex)."""
    return verify_key.encode(encoder=HexEncoder).decode()[:16]


class AuditLogger:
    """Append-only audit logger with cryptographic signatures"""
    
    def __init__(self, postgres_url: str, signing_key: Optional[bytes] = None):
        self.postgres_url = postgres_url
        self.pool: Optional[asyncpg.Pool] = None

        # Initialize or load signing key.
        # Explicit signing_key arg wins (used by tests); otherwise load from
        # env/file/ephemeral via load_signing_key().
        if signing_key:
            self.signing_key = SigningKey(signing_key)
            self.key_is_persistent = True
        else:
            self.signing_key, self.key_is_persistent = load_signing_key()

        self.verify_key = self.signing_key.verify_key
        self.key_id = _key_id_for(self.verify_key)

        # Map of key_id -> VerifyKey so records signed by prior keys can still be
        # verified as long as their public keys are known. Seed with the active
        # key plus any SHACKLE_KNOWN_VERIFY_KEYS (comma-separated hex/base64
        # public keys).
        self.known_verify_keys: Dict[str, VerifyKey] = {self.key_id: self.verify_key}
        extra = os.getenv("SHACKLE_KNOWN_VERIFY_KEYS", "")
        for token in [t for t in extra.split(",") if t.strip()]:
            try:
                vk = VerifyKey(_decode_key_material(token))
                self.known_verify_keys[_key_id_for(vk)] = vk
            except Exception as e:
                logger.error("Ignoring invalid SHACKLE_KNOWN_VERIFY_KEYS entry: %s", e)

        logger.info(
            "Audit signing key id=%s persistent=%s pub=%s",
            self.key_id,
            self.key_is_persistent,
            self.verify_key.encode(encoder=HexEncoder).decode(),
        )
    
    async def connect(self):
        """Connect to Postgres and initialize schema"""
        try:
            self.pool = await asyncpg.create_pool(
                self.postgres_url,
                min_size=2,
                max_size=10
            )
            
            # Initialize schema
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS audit_log (
                        id BIGSERIAL PRIMARY KEY,
                        timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        event_type VARCHAR(50) NOT NULL,
                        session_id VARCHAR(255) NOT NULL,
                        tool_name VARCHAR(255),
                        decision VARCHAR(20),
                        reason TEXT,
                        parameters JSONB,
                        result JSONB,
                        error TEXT,
                        cost DECIMAL(10, 6),
                        execution_time_ms DECIMAL(10, 2),
                        signature TEXT NOT NULL,
                        key_id VARCHAR(32),
                        metadata JSONB
                    );

                    -- Migration for tables created before key_id existed.
                    ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS key_id VARCHAR(32);

                    CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp DESC);
                    CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log(session_id);
                    CREATE INDEX IF NOT EXISTS idx_audit_tool ON audit_log(tool_name);
                    CREATE INDEX IF NOT EXISTS idx_audit_event_type ON audit_log(event_type);
                    CREATE INDEX IF NOT EXISTS idx_audit_key_id ON audit_log(key_id);
                """)
            
            logger.info("Connected to Postgres and initialized schema")
            
        except Exception as e:
            logger.error(f"Failed to connect to Postgres: {e}")
            raise
    
    async def close(self):
        """Close Postgres connection"""
        if self.pool:
            await self.pool.close()
            logger.info("Closed Postgres connection")
    
    def is_connected(self) -> bool:
        """Check if connected to Postgres"""
        return self.pool is not None
    
    def _sign_record(self, record: Dict) -> str:
        """Sign a record with Ed25519"""
        # Create canonical JSON representation
        record_json = json.dumps(record, sort_keys=True, separators=(',', ':'))
        record_bytes = record_json.encode('utf-8')
        
        # Sign
        signed = self.signing_key.sign(record_bytes)
        
        # Return signature as hex
        return signed.signature.hex()
    
    async def log_decision(
        self,
        session_id: str,
        tool_name: str,
        decision: str,
        reason: Optional[str] = None,
        metadata: Optional[Dict] = None
    ):
        """Log a pre-execution decision"""
        try:
            timestamp = datetime.utcnow()
            
            record = {
                "timestamp": timestamp.isoformat(),
                "event_type": "decision",
                "session_id": session_id,
                "tool_name": tool_name,
                "decision": decision,
                "reason": reason,
                "metadata": metadata or {}
            }
            
            signature = self._sign_record(record)
            
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO audit_log (
                        timestamp, event_type, session_id, tool_name,
                        decision, reason, signature, key_id, metadata
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """, timestamp, "decision", session_id, tool_name,
                    decision, reason, signature, self.key_id, json.dumps(metadata or {}))
            
            logger.info(f"Logged decision: {session_id} | {tool_name} | {decision}")
            
        except Exception as e:
            logger.error(f"Error logging decision: {e}", exc_info=True)
    
    async def log_execution(
        self,
        session_id: str,
        tool_name: str,
        parameters: Dict,
        result: Optional[Dict] = None,
        error: Optional[str] = None,
        cost: float = 0.0,
        execution_time_ms: float = 0.0,
        metadata: Optional[Dict] = None
    ):
        """Log a post-execution result"""
        try:
            timestamp = datetime.utcnow()
            
            record = {
                "timestamp": timestamp.isoformat(),
                "event_type": "execution",
                "session_id": session_id,
                "tool_name": tool_name,
                "parameters": parameters,
                "result": result,
                "error": error,
                "cost": cost,
                "execution_time_ms": execution_time_ms,
                "metadata": metadata or {}
            }
            
            signature = self._sign_record(record)
            
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO audit_log (
                        timestamp, event_type, session_id, tool_name,
                        parameters, result, error, cost, execution_time_ms,
                        signature, key_id, metadata
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                """, timestamp, "execution", session_id, tool_name,
                    json.dumps(parameters), json.dumps(result) if result else None,
                    error, cost, execution_time_ms, signature, self.key_id,
                    json.dumps(metadata or {}))
            
            logger.info(f"Logged execution: {session_id} | {tool_name} | cost={cost}")
            
        except Exception as e:
            logger.error(f"Error logging execution: {e}", exc_info=True)
    
    async def verify_log_integrity(self, log_id: int) -> bool:
        """Verify the signature of a log entry"""
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    SELECT timestamp, event_type, session_id, tool_name,
                           decision, reason, parameters, result, error,
                           cost, execution_time_ms, signature, key_id, metadata
                    FROM audit_log WHERE id = $1
                """, log_id)
            
            if not row:
                return False
            
            # Reconstruct record
            record = {
                "timestamp": row["timestamp"].isoformat(),
                "event_type": row["event_type"],
                "session_id": row["session_id"],
                "tool_name": row["tool_name"],
            }
            
            if row["decision"]:
                record["decision"] = row["decision"]
            if row["reason"]:
                record["reason"] = row["reason"]
            if row["parameters"]:
                record["parameters"] = json.loads(row["parameters"])
            if row["result"]:
                record["result"] = json.loads(row["result"])
            if row["error"]:
                record["error"] = row["error"]
            if row["cost"]:
                record["cost"] = float(row["cost"])
            if row["execution_time_ms"]:
                record["execution_time_ms"] = float(row["execution_time_ms"])
            if row["metadata"]:
                record["metadata"] = json.loads(row["metadata"])
            
            # Verify signature
            record_json = json.dumps(record, sort_keys=True, separators=(',', ':'))
            record_bytes = record_json.encode('utf-8')
            
            signature_bytes = bytes.fromhex(row["signature"])

            # Select the verify key that signed this row. Rows written by prior
            # (persistent) keys still verify as long as their public key is known
            # via SHACKLE_KNOWN_VERIFY_KEYS. Legacy rows with no key_id fall back
            # to the active key (best effort).
            row_key_id = row["key_id"]
            if row_key_id:
                verify_key = self.known_verify_keys.get(row_key_id)
                if verify_key is None:
                    logger.warning(
                        "No known verify key for key_id=%s (record id=%s); "
                        "cannot verify. Add its public key to SHACKLE_KNOWN_VERIFY_KEYS.",
                        row_key_id, log_id,
                    )
                    return False
            else:
                verify_key = self.verify_key

            try:
                verify_key.verify(record_bytes, signature_bytes)
                return True
            except Exception:
                return False
                
        except Exception as e:
            logger.error(f"Error verifying log integrity: {e}", exc_info=True)
            return False
    
    async def get_session_logs(self, session_id: str, limit: int = 100):
        """Get recent logs for a session"""
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT id, timestamp, event_type, tool_name, decision,
                           reason, cost, execution_time_ms, error
                    FROM audit_log
                    WHERE session_id = $1
                    ORDER BY timestamp DESC
                    LIMIT $2
                """, session_id, limit)
            
            return [dict(row) for row in rows]
            
        except Exception as e:
            logger.error(f"Error getting session logs: {e}", exc_info=True)
            return []
    
    async def get_stats(self) -> Dict:
        """Get aggregate statistics"""
        try:
            async with self.pool.acquire() as conn:
                total = await conn.fetchval("SELECT COUNT(*) FROM audit_log")
                
                by_type = await conn.fetch("""
                    SELECT event_type, COUNT(*) as count
                    FROM audit_log
                    GROUP BY event_type
                """)
                
                by_decision = await conn.fetch("""
                    SELECT decision, COUNT(*) as count
                    FROM audit_log
                    WHERE decision IS NOT NULL
                    GROUP BY decision
                """)
                
                total_cost = await conn.fetchval("""
                    SELECT COALESCE(SUM(cost), 0) FROM audit_log
                """)
            
            return {
                "total_logs": total,
                "by_type": {row["event_type"]: row["count"] for row in by_type},
                "by_decision": {row["decision"]: row["count"] for row in by_decision},
                "total_cost": float(total_cost)
            }
            
        except Exception as e:
            logger.error(f"Error getting stats: {e}", exc_info=True)
            return {}
