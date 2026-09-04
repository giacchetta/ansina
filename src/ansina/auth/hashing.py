"""Two credential-hashing paths, deliberately different (issue #24).

Passwords are low-entropy, human-chosen secrets — they get argon2id, a slow memory-hard
KDF tuned via `[security.password]` (`config.settings.PasswordHashSettings`), because a
stolen hash must stay expensive to brute-force offline.

API tokens are high-entropy, machine-generated secrets that `ansina.api.auth
.BearerAuthMiddleware` verifies on *every* authenticated request — paying argon2's
deliberate work factor on that hot path buys nothing (there is no brute-force risk to
defend against; the token itself is the entropy) and would cost tens of milliseconds per
request. Tokens get a per-row salt plus plain SHA-256, compared with
`hmac.compare_digest` — constant-time, same discipline the rest of this codebase applies
to any secret comparison — looked up by scanning `credentials` rows.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

if TYPE_CHECKING:
    from ansina.config.settings import Settings

# `secrets.token_hex(32)` -> 64 hex chars -> 256 bits of salt. Plenty for a value that
# only needs to make two identical tokens hash differently, not resist its own attack.
_TOKEN_SALT_BYTES = 32


@dataclass(frozen=True, slots=True)
class Argon2Params:
    """Tunable argon2id work factors, sourced from `Settings.security.password`.

    A separate type (rather than passing `Settings` straight to `hash_password`) so
    tests can construct cheap params directly without loading a full `Settings` tree —
    the unit suite uses minimal values so hashing doesn't dominate test runtime.
    """

    time_cost: int
    memory_cost_kib: int
    parallelism: int

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        password = settings.security.password
        return cls(
            time_cost=password.time_cost,
            memory_cost_kib=password.memory_cost_kib,
            parallelism=password.parallelism,
        )


def _hasher(params: Argon2Params) -> PasswordHasher:
    return PasswordHasher(
        time_cost=params.time_cost,
        memory_cost=params.memory_cost_kib,
        parallelism=params.parallelism,
    )


def hash_password(raw: str, params: Argon2Params) -> str:
    """A PHC-format argon2id hash string (salt embedded) — never the raw password."""
    return _hasher(params).hash(raw)


def verify_password(raw: str, stored_hash: str, params: Argon2Params) -> bool:
    """`True` iff `raw` matches `stored_hash`. Never raises — a wrong password
    (`VerifyMismatchError`, a `VerificationError` subclass) and a malformed/corrupt
    `stored_hash` (`InvalidHashError`, which argon2-cffi does *not* nest under
    `VerificationError`) are both expected outcomes here, not error conditions callers
    must catch.
    """
    try:
        return _hasher(params).verify(stored_hash, raw)
    except VerificationError, InvalidHashError:
        return False


def password_needs_rehash(stored_hash: str, params: Argon2Params) -> bool:
    """`True` if `stored_hash` was hashed under different work factors than `params`
    currently specifies — e.g. `[security.password]` was tuned up after the hash was
    written. Callers rehash on the next successful `verify_password` when this is
    `True`.
    """
    return _hasher(params).check_needs_rehash(stored_hash)


def new_token_salt() -> str:
    """A fresh per-credential salt for `hash_token` — one call per issued API token."""
    return secrets.token_hex(_TOKEN_SALT_BYTES)


def hash_token(token: str, salt: str) -> str:
    """Salted SHA-256 of `token`, hex-encoded. Deterministic given the same salt, so a
    stored `(salt, hash)` pair can be re-derived from a presented token for comparison.
    """
    return hashlib.sha256((salt + token).encode("utf-8")).hexdigest()


def verify_token_hash(candidate: str, salt: str, stored_hash: str) -> bool:
    """Constant-time comparison of a re-derived hash against `stored_hash` — never `==`
    on secret material.
    """
    return hmac.compare_digest(hash_token(candidate, salt), stored_hash)
