"""Password hashing, JWT issuing and one-time token helpers."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt

from .config import settings

PBKDF2_ALGO = "pbkdf2_sha256"


# --------------------------------------------------------------------------
# time helpers (naive UTC everywhere, so MySQL DATETIME stays consistent)
# --------------------------------------------------------------------------
def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------
# password hashing - PBKDF2-HMAC-SHA256 (no native build deps required)
# --------------------------------------------------------------------------
def hash_password(password: str, iterations: int | None = None) -> str:
    iterations = iterations or settings.password_hash_iterations
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "$".join([
        PBKDF2_ALGO,
        str(iterations),
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    ])


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_b64, digest_b64 = stored.split("$")
        if algo != PBKDF2_ALGO:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
        return hmac.compare_digest(expected, actual)
    except Exception:  # noqa: BLE001
        return False


def password_fingerprint(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()[:12]


def validate_password_strength(password: str) -> list[str]:
    problems = []
    if len(password) < 8:
        problems.append("must be at least 8 characters")
    if not any(c.isupper() for c in password):
        problems.append("must contain an uppercase letter")
    if not any(c.islower() for c in password):
        problems.append("must contain a lowercase letter")
    if not any(c.isdigit() for c in password):
        problems.append("must contain a digit")
    return problems


# --------------------------------------------------------------------------
# JWT
# --------------------------------------------------------------------------
def _encode(payload: dict) -> str:
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


def create_access_token(user_id: int, role: str, email: str, extra: dict | None = None) -> tuple[str, str, datetime]:
    jti = secrets.token_urlsafe(24)
    expires_at = utcnow() + timedelta(minutes=settings.access_token_minutes)
    payload = {
        "sub": str(user_id),
        "role": role,
        "email": email,
        "type": "access",
        "jti": jti,
        "iat": utcnow(),
        "exp": expires_at,
    }
    if extra:
        payload.update(extra)
    return _encode(payload), jti, expires_at


def create_refresh_token(user_id: int, session_id: str) -> tuple[str, datetime]:
    expires_at = utcnow() + timedelta(days=settings.refresh_token_days)
    token = _encode({
        "sub": str(user_id),
        "type": "refresh",
        "sid": session_id,
        "iat": utcnow(),
        "exp": expires_at,
    })
    return token, expires_at


def new_opaque_token() -> str:
    return secrets.token_urlsafe(40)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
