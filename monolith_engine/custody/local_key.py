"""Local custody — Ed25519 keypair + run-token mint/verify from a local keystore.

The single-node carve of the hosted run-token machinery: keys live in an
owner-only local file (never a cloud secret manager), tokens are short-lived
EdDSA JWTs scoping what a run may do. The hosted service implements the same
mint/verify against its GSM-held key; the token format is identical.
"""

from __future__ import annotations

import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ALGORITHM = "EdDSA"
DEFAULT_TTL_SECONDS = 900


def generate_keypair() -> tuple[str, str]:
    """Generate an Ed25519 keypair as (private_pem, public_pem)."""
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


def ensure_local_key(path: str | Path) -> str:
    """Load the signing key from an owner-only local file, generating on first use.

    Fail-closed on permissions: a group/world-readable keystore is refused.
    """
    p = Path(path)
    if p.is_file():
        mode = stat.S_IMODE(p.stat().st_mode)
        if mode & 0o077:
            raise PermissionError(
                f"local keystore '{p}' must not be group/world accessible (mode {oct(mode)})"
            )
        return p.read_text()
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_pem, _ = generate_keypair()
    p.write_text(private_pem)
    p.chmod(0o600)
    return private_pem


def mint_run_token(
    private_pem: str,
    *,
    tenant_id: str,
    container_id: str,
    scopes: list[str] | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": container_id,
        "tenant": tenant_id,
        "scopes": scopes or [],
        "iat": now,
        "exp": now + timedelta(seconds=ttl_seconds),
        "iss": "monolith-engine",
    }
    return jwt.encode(payload, private_pem, algorithm=ALGORITHM)


def verify_run_token(token: str, private_pem: str) -> dict:
    """Verify signature + expiry; returns the claims. Raises jwt exceptions on failure."""
    public_key = (
        serialization.load_pem_private_key(private_pem.encode(), password=None)
        .public_key()
    )
    return jwt.decode(token, public_key, algorithms=[ALGORITHM], issuer="monolith-engine")


__all__ = ["ensure_local_key", "generate_keypair", "mint_run_token", "verify_run_token", "ALGORITHM"]
