#!/usr/bin/env python3
"""
SHACKLE-V2 License Key Generator
Generates cryptographically secure enterprise licenses with validation checksums
"""

import hashlib
import hmac
import logging
import os
import secrets
import sys
import uuid
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization
import base64

logger = logging.getLogger(__name__)

class LicenseGenerator:
    """Generate SHACKLE-ENT licenses with crypto validation"""
    
    def __init__(self, master_secret: Optional[str] = None):
        """
        Initialize generator with master secret for HMAC validation
        
        Args:
            master_secret: Master secret for license validation (keep secure!)
        """
        if master_secret:
            self.master_secret = master_secret.encode()
            self.generated_new = False
        else:
            # Generate a new master secret if none provided.
            # SECURITY: never print the secret here. Emitting it to stdout leaks
            # it into CI logs, container stdout, shell scrollback, and log
            # aggregators. The CLI persists it to a 0600 file (or shows it only
            # under an explicit --show-secret). Library callers read it via
            # export_master_secret().
            self.master_secret = secrets.token_hex(32).encode()
            self.generated_new = True
            logger.warning(
                "Generated a new master secret. Capture it from the CLI secret "
                "file (or --show-secret); it is required for license validation."
            )
        
        # Generate Ed25519 signing key pair
        self.private_key = ed25519.Ed25519PrivateKey.generate()
        self.public_key = self.private_key.public_key()
    
    def generate_license(
        self,
        customer_name: str,
        tier: str = "ENTERPRISE",
        duration_days: int = 365,
        max_nodes: Optional[int] = None,
        features: Optional[list] = None,
        node_binding: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Generate a new SHACKLE enterprise license
        
        Args:
            customer_name: Customer/organization name
            tier: License tier (ENTERPRISE, SOVEREIGN, UNLIMITED)
            duration_days: License validity period
            max_nodes: Maximum allowed nodes (None = unlimited)
            features: List of enabled features
            node_binding: Optional node hardware ID for binding
            
        Returns:
            Complete license object with key, metadata, and signature
        """
        license_id = str(uuid.uuid4())
        issued_at = datetime.utcnow()
        expires_at = issued_at + timedelta(days=duration_days)
        
        # Build license metadata
        metadata = {
            "customer": customer_name,
            "tier": tier,
            "max_nodes": max_nodes,
            "features": features or ["proxy", "audit", "compliance_export"],
            "issued_at": issued_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "node_binding": node_binding
        }
        
        # Generate checksum: HMAC-SHA256 of license_id + metadata
        checksum_input = f"{license_id}:{json.dumps(metadata, sort_keys=True)}"
        checksum = hmac.new(
            self.master_secret,
            checksum_input.encode(),
            hashlib.sha256
        ).hexdigest()[:16]  # Use first 16 chars for readability
        
        # Construct license key: SHACKLE-ENT-{UUID}-{CHECKSUM}
        license_key = f"SHACKLE-ENT-{license_id}-{checksum}"
        
        # Sign the complete license with Ed25519
        signature_payload = f"{license_key}:{json.dumps(metadata, sort_keys=True)}"
        signature = self.private_key.sign(signature_payload.encode())
        signature_b64 = base64.b64encode(signature).decode()
        
        return {
            "license_key": license_key,
            "license_id": license_id,
            "checksum": checksum,
            "metadata": metadata,
            "signature": signature_b64,
            "public_key": base64.b64encode(
                self.public_key.public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw
                )
            ).decode()
        }
    
    def export_public_key(self) -> str:
        """Export public key for license verification"""
        public_bytes = self.public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        )
        return public_bytes.decode()
    
    def export_master_secret(self) -> str:
        """Export master secret (KEEP SECURE!)"""
        return self.master_secret.decode()


def generate_node_certificate(
    license_key: str,
    node_id: str,
    hardware_id: str,
    private_key: ed25519.Ed25519PrivateKey
) -> Dict[str, str]:
    """
    Generate node-bound certificate for hardware binding
    
    Args:
        license_key: Valid SHACKLE license key
        node_id: Unique node identifier
        hardware_id: Hardware fingerprint (MAC, CPU ID, etc.)
        private_key: Ed25519 private key for signing
        
    Returns:
        Node certificate with signature
    """
    cert_data = {
        "license_key": license_key,
        "node_id": node_id,
        "hardware_id": hardware_id,
        "issued_at": datetime.utcnow().isoformat(),
        "cert_version": "1.0"
    }
    
    # Sign certificate
    cert_json = json.dumps(cert_data, sort_keys=True)
    signature = private_key.sign(cert_json.encode())
    
    return {
        "certificate": cert_data,
        "signature": base64.b64encode(signature).decode()
    }


def main():
    """CLI for license generation"""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="SHACKLE-V2 License Key Generator"
    )
    parser.add_argument("customer", help="Customer/organization name")
    parser.add_argument(
        "--tier",
        choices=["ENTERPRISE", "SOVEREIGN", "UNLIMITED"],
        default="ENTERPRISE",
        help="License tier"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=365,
        help="License duration in days"
    )
    parser.add_argument(
        "--max-nodes",
        type=int,
        help="Maximum nodes (omit for unlimited)"
    )
    parser.add_argument(
        "--features",
        nargs="+",
        help="Enabled features"
    )
    parser.add_argument(
        "--node-binding",
        help="Hardware ID for node binding"
    )
    parser.add_argument(
        "--master-secret",
        help="Master secret (will generate if not provided)"
    )
    parser.add_argument(
        "--output",
        help="Output JSON file path (license bundle only; never contains the master secret)"
    )
    parser.add_argument(
        "--secret-out",
        help="Path to write the master secret to (created mode 0600). "
             "Defaults to <output>.secret, or shackle-master-secret-<license_id>.key."
    )
    parser.add_argument(
        "--show-secret",
        action="store_true",
        help="Print the master secret to stderr instead of writing it to a file. "
             "Off by default so the secret never lands in stdout/logs."
    )
    
    args = parser.parse_args()
    
    # Initialize generator
    generator = LicenseGenerator(master_secret=args.master_secret)
    
    # Generate license
    license_data = generator.generate_license(
        customer_name=args.customer,
        tier=args.tier,
        duration_days=args.days,
        max_nodes=args.max_nodes,
        features=args.features,
        node_binding=args.node_binding
    )
    
    # Output — the license bundle NEVER contains the master secret. The secret
    # is a separate credential and is handled out-of-band below.
    output = {
        "license": license_data,
        "public_key_pem": generator.export_public_key()
    }
    
    if args.output:
        with open(args.output, 'w') as f:
            json.dump(output, f, indent=2)
        print(f"✅ License written to {args.output}")
    else:
        print(json.dumps(output, indent=2))
    
    # Master secret handling — never on stdout. Show on stderr only when the
    # caller explicitly opts in; otherwise persist to a 0600 file. A secret that
    # was supplied by the caller is neither re-shown nor re-persisted.
    secret = generator.export_master_secret()
    if args.show_secret:
        print(f"MASTER_SECRET={secret}", file=sys.stderr)
    elif generator.generated_new or args.secret_out:
        secret_path = args.secret_out or (
            f"{args.output}.secret" if args.output
            else f"shackle-master-secret-{license_data['license_id']}.key"
        )
        fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as sf:
            sf.write(secret + "\n")
        print(
            f"🔐 Master secret written to {secret_path} (mode 0600). Move it into "
            f"your secrets manager; it is required for license validation.",
            file=sys.stderr,
        )
    
    # Pretty print key (the license key is not a secret)
    print(f"\n🔑 LICENSE KEY:\n{license_data['license_key']}\n")


if __name__ == "__main__":
    main()
