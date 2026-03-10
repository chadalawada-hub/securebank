"""
security.py – Cryptographic utilities.

Password strategy:
  • SHA-256 hash of the raw password is computed client-side (documented
    in API contract) before transmission – this acts as a pre-hash step.
  • The SHA-256 hex string is then hashed with bcrypt (cost=12) server-side
    before storage, providing both deterministic lookup capability AND
    bcrypt's salted, slow-hash brute-force resistance.
  • At rest in PostgreSQL: only the bcrypt digest is stored, never the
    original password or the SHA-256 pre-hash in plaintext.

JWT strategy:
  • Short-lived access tokens (30 min default).
  • Long-lived refresh tokens stored as SHA-256(token) in DB so the raw
    refresh token is never persisted.
"""
import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt
from passlib.context import CryptContext

from backend.config import get_settings

settings = get_settings()

# bcrypt cost = 12  (NIST SP 800-63b recommends ≥ 10)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)


# ─── Hashing ─────────────────────────────────────────────────────────────────

def sha256_hex(value: str) -> str:
    """Deterministic SHA-256 hex digest – used for lookup hashes."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_password(raw_password: str) -> str:
    """
    Two-stage password hashing:
      1. SHA-256 pre-hash (normalises length for bcrypt's 72-char limit)
      2. bcrypt(cost=12) for slow, salted storage
    """
    pre_hash = sha256_hex(raw_password)
    return pwd_context.hash(pre_hash)


def verify_password(raw_password: str, stored_hash: str) -> bool:
    """Verify *raw_password* against a stored bcrypt hash."""
    pre_hash = sha256_hex(raw_password)
    return pwd_context.verify(pre_hash, stored_hash)


# ─── Password Policy ─────────────────────────────────────────────────────────

# NIST SP 800-63B + OWASP ASVS Level 2 policy
PASSWORD_MIN_LENGTH    = 12
PASSWORD_MAX_LENGTH    = 128

# Minimum character-class requirements
_RE_UPPER    = re.compile(r"[A-Z]")
_RE_LOWER    = re.compile(r"[a-z]")
_RE_DIGIT    = re.compile(r"\d")
_RE_SPECIAL  = re.compile(r"[!@#$%^&*()_+\-=\[\]{};':\"\\|,.<>\/?`~]")
_RE_SEQUENCE = re.compile(r"(.)\1{2,}")          # 3+ repeated chars
_RE_COMMON   = re.compile(                       # keyboard walks & common words
    r"(?:password|passwd|qwerty|123456|letmein|welcome|admin|login|abc123)",
    re.IGNORECASE,
)

DISALLOWED_PATTERNS = [
    ("3+ consecutive repeated characters", _RE_SEQUENCE),
    ("common password pattern", _RE_COMMON),
]


def validate_password_strength(password: str) -> tuple[bool, list[str]]:
    """
    Validate password against the banking security policy.
    Returns (is_valid: bool, errors: list[str]).
    """
    errors: list[str] = []

    if len(password) < PASSWORD_MIN_LENGTH:
        errors.append(f"Must be at least {PASSWORD_MIN_LENGTH} characters.")
    if len(password) > PASSWORD_MAX_LENGTH:
        errors.append(f"Must not exceed {PASSWORD_MAX_LENGTH} characters.")
    if not _RE_UPPER.search(password):
        errors.append("Must contain at least one uppercase letter (A-Z).")
    if not _RE_LOWER.search(password):
        errors.append("Must contain at least one lowercase letter (a-z).")
    if not _RE_DIGIT.search(password):
        errors.append("Must contain at least one digit (0-9).")
    if not _RE_SPECIAL.search(password):
        errors.append("Must contain at least one special character (!@#$%^&* …).")

    for label, pattern in DISALLOWED_PATTERNS:
        if pattern.search(password):
            errors.append(f"Password must not contain {label}.")

    return (len(errors) == 0, errors)


# ─── JWT ─────────────────────────────────────────────────────────────────────

def create_access_token(
    subject: str,
    extra_claims: Optional[dict] = None,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """
    Create a signed JWT access token.

    Payload:
      sub   – user UUID (string)
      exp   – expiry timestamp
      iat   – issued-at timestamp
      type  – "access"
      + any extra_claims (e.g. {"role": "customer"})
    """
    now    = datetime.now(timezone.utc)
    expire = now + (expires_delta or timedelta(minutes=settings.access_token_expire_minutes))

    payload = {
        "sub":  str(subject),
        "exp":  expire,
        "iat":  now,
        "type": "access",
        **(extra_claims or {}),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def create_refresh_token() -> tuple[str, str]:
    """
    Generate a cryptographically-random refresh token.
    Returns (raw_token, sha256_hash_for_db_storage).
    The raw token is sent to the client; only the hash is persisted.
    """
    raw   = secrets.token_urlsafe(64)
    hashed = sha256_hex(raw)
    return raw, hashed


def decode_access_token(token: str) -> dict:
    """
    Decode and validate a JWT access token.
    Raises JWTError on invalid/expired token.
    """
    payload = jwt.decode(
        token,
        settings.secret_key,
        algorithms=[settings.algorithm],
    )
    if payload.get("type") != "access":
        raise JWTError("Invalid token type")
    return payload


# ─── CSRF / State tokens ─────────────────────────────────────────────────────

def generate_oauth_state() -> str:
    """Generate a random CSRF state token for OAuth flows."""
    return secrets.token_urlsafe(32)
