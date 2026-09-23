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

`dummy_password_hash` (issue #50) is a third thing this module owns for a narrower
reason: `POST /auth/login` must return the identical 401 in the identical *time* for an
unknown username as for a real one with a wrong password, or the response clock itself
becomes a user-enumeration oracle — a known-username guess pays a real argon2id verify
(tens of milliseconds under the OWASP-baseline defaults) while an unknown one would
otherwise short-circuit before any hashing runs at all. A throwaway hash to verify
against on that short-circuit path equalizes the two.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Self

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

if TYPE_CHECKING:
    from ansina.config.settings import Settings

# `secrets.token_hex(32)` -> 64 hex chars -> 256 bits of salt. Plenty for a value that
# only needs to make two identical tokens hash differently, not resist its own attack.
_TOKEN_SALT_BYTES = 32

# 32 raw bytes of secret the dummy hash below is generated from — never compared
# against, just needs to be unguessable so `dummy_password_hash`'s output can never
# accidentally match a real submitted password.
_DUMMY_SECRET_BYTES = 32


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


@lru_cache(maxsize=1)
def dummy_password_hash(params: Argon2Params) -> str:
    """A throwaway argon2id hash, generated once per `params` and cached for the
    process lifetime, that no submitted password can ever match — for a caller (issue
    #50's `POST /auth/login`) that must pay a `verify_password` call's cost on a path
    where there's no real stored hash to check against, so that path takes the same
    time as one that does. `Argon2Params` is a frozen, `slots=True` dataclass, so it's
    hashable and safe to cache on; keyed on `params` (not cached bare) because
    argon2-cffi reads its work factors back out of the stored PHC string at verify
    time, so a dummy generated under different params would equalize nothing.
    """
    return hash_password(secrets.token_urlsafe(_DUMMY_SECRET_BYTES), params)


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
