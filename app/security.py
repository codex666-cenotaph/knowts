"""Password hashing and opaque-token helpers.

- Passwords: argon2id via ``argon2-cffi``.
- Session/CSRF tokens: cryptographically-random URL-safe strings. The session
  cookie carries the raw token; only its SHA-256 is stored in the DB, so a
  database leak does not hand out usable session cookies.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except (InvalidHashError, ValueError):
        return False


def new_token(nbytes: int = 32) -> str:
    """A fresh URL-safe random token (used for session ids and CSRF tokens)."""
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """Stable SHA-256 of a token, hex-encoded — the value stored in the DB."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_equal(a: str, b: str) -> bool:
    """Constant-time comparison for CSRF tokens."""
    return hmac.compare_digest(a, b)
