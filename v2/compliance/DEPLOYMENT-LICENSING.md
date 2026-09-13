# Licensing Deployment Guide

The license server verifies signatures against **server-side trust anchors** and
**fails closed**. That is deliberate: an unconfigured server rejects every
license rather than accepting forged ones. It also means a deployment is not
finished until the four variables below are set.

## The four secrets

| Variable | Holds | Secret? |
|---|---|---|
| `SHACKLE_MASTER_SECRET` | HMAC secret for license checksums | **Yes** |
| `SHACKLE_LICENSE_PUBKEYS` | `{"<key_id>":"<base64 Ed25519 public key>"}` the server trusts | No, but integrity-critical |
| `SHACKLE_LICENSE_PRIVATE_KEY` | base64 Ed25519 issuer private seed, for minting licenses | **Yes** |
| `SHACKLE_LICENSE_SIGNING_KEY_ID` | `key_id` paired with the private key | No |

`MASTER_SECRET` is still accepted as a legacy alias of `SHACKLE_MASTER_SECRET`.

Only the issuing host needs `SHACKLE_LICENSE_PRIVATE_KEY`. A server that merely
validates licenses needs the master secret and the public keys.

## 1. Mint a stable issuer identity

Run this **once**, on the machine that will own the secrets:

```bash
python license_keygen.py --bootstrap-issuer
```

It writes `shackle-license-<key_id>.env` with mode `0600` containing all four
variables. Secret values never go to stdout, so they stay out of CI logs, shell
history and log aggregators.

The issuer key must be **stable**. The keygen refuses to run without one rather
than minting a throwaway key per invocation, because licenses signed by an
ephemeral key cannot be verified by anyone, and that failure would otherwise
surface at the paying customer instead of at the operator.

## 2. Load the secrets into the deployment

**docker-compose** (the compose file already passes all four through):

```bash
docker compose --env-file ./shackle-license-<key_id>.env up -d license-server
```

**systemd** (`shackle-license-server.service` reads `EnvironmentFile`):

```bash
install -m 600 shackle-license-<key_id>.env /opt/shackle-v2/.env
systemctl restart shackle-license-server
```

**Managed platform**: paste each variable into the platform's secret manager.

Then delete the local env file. Do not commit it. `.env` is already gitignored,
but the file name above is not, so move it and remove it.

## 3. Verify the deployment

```bash
curl -s http://<host>:8000/health
```

```json
{
  "status": "healthy",
  "licensing_ready": true,
  "trust_anchors": 1,
  "master_secret_configured": true,
  "signing_key_configured": true
}
```

`licensing_ready: false` means the server is up but rejecting every license.
The startup log names the missing variables (never their values). `/health`
reports counts and booleans only, never key material.

## 4. Issue a license

```bash
python license_keygen.py "Customer Name" --tier ENTERPRISE --days 365 \
  --output customer.json
```

The bundle includes `key_id`. Send it when registering:

```bash
curl -X POST http://<host>:8000/api/v1/licenses/register \
  -H 'Content-Type: application/json' \
  -d '{"license_key":"...","metadata":{...},"signature":"...","key_id":"..."}'
```

`public_key` is no longer accepted for verification. Any client-supplied key is
recorded as `claimed_public_key` for audit only. This is the whole point of the
fix: a caller-supplied key let anyone sign their own license and have it pass.

## 5. Rotation

`SHACKLE_LICENSE_PUBKEYS` holds a map, so several keys can be trusted at once:

```json
{"issuer-20260101-a1b2c3":"<old pubkey>","issuer-20270101-d4e5f6":"<new pubkey>"}
```

1. Bootstrap a new identity and add its public key alongside the current one.
2. Switch the issuing host's private key and `key_id` to the new pair.
3. Once no live license depends on the old `key_id`, remove it.

Rotate immediately if a private key was ever pasted into a chat tool, an issue
tracker, a support ticket, or any other system you do not control.

## Tests

```bash
pytest v2/compliance/test_license_signature_trust.py -v
```

Covers forgery rejection, fail-closed behavior with no trust configured,
startup trust loading, a genuine issue/register/validate round trip, issuer key
stability, and rejection of a forged signature that carries a valid checksum.
