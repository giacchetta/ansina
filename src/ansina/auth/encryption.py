"""AES-GCM envelope encryption for TOTP secrets at rest. See issue #41.

A TOTP secret is the one credential this codebase cannot hash (`ansina.auth.hashing`'s
docstring, by contrast): `TotpStepUpVerifier.verify` needs the plaintext seed back to
compute the current code, which a one-way hash structurally cannot give it. Reversible
by construction is a smaller attack surface than "hashed, but useless" — this stores a
versioned AES-GCM envelope instead, keyed by `[security.encryption] key`
(`config.settings.EncryptionSettings`) — env-only, like every other secret in this
codebase, with no rotation path built: losing or rotating the key means the affected
enrollments are cleared (`DELETE /auth/users/{id}/totp`) and the user re-enrolls,
documented rather than silently unsupported (M3's open consideration #3).
"""

from __future__ import annotations

import base64
import os
from typing import TYPE_CHECKING, ClassVar

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ansina.auth.models import CredentialType
from ansina.auth.repositories import CredentialRepository
from ansina.errors import AuthError

if TYPE_CHECKING:
    from ansina.config.settings import Settings
    from ansina.storage.database import Database

_ENVELOPE_VERSION = "v1"
# 96 bits — the nonce size AES-GCM is designed for; anything else forces GHASH to hash
# the nonce down to 96 bits first, which is best avoided rather than relied on.
_NONCE_BYTES = 12


class EncryptionKeyMissingError(AuthError):
    """No `[security.encryption] key` is configured, but it's needed:
    `ensure_key_configured_if_needed` raises this at boot (the `HeartUnavailableError`
    fail-loudly pattern) if any `totp` credential row already exists with no key to
    decrypt it, and `ansina.api.routes.me`'s enroll route raises it at request time if
    a fresh enrollment is attempted with none configured — both cases would otherwise
    surface as either a silent, permanent step-up failure or an unhandled 500.
    """

    code: ClassVar[str] = "ansina.auth.encryption_key_missing"


class DecryptionError(AuthError):
    """A stored TOTP envelope failed to decrypt under the configured key — a rotated
    or lost key (no re-encryption path is built, see the module docstring) or a
    corrupted/tampered stored value. Never allowed to escape `TotpStepUpVerifier
    .verify`, which treats it as a plain non-match rather than a distinct error, the
    same discipline `PasswordStepUpVerifier` already follows for a malformed payload.
    """

    code: ClassVar[str] = "ansina.auth.decryption_failed"


def resolve_key(settings: Settings) -> bytes | None:
    """The configured AES-256 key as raw bytes, or `None` if unset.

    Decodes the same urlsafe-base64 shape `config.settings.EncryptionSettings` already
    validated at load time — this never raises on a value that reached here, since an
    invalid one would already have failed `load_settings()` itself.
    """
    secret = settings.security.encryption.key
    if secret is None:
        return None
    raw = secret.get_secret_value()
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def encrypt(plaintext: bytes, key: bytes) -> str:
    """A versioned envelope: `v1:<nonce_b64>:<ciphertext_b64>`. The nonce is fresh on
    every call (`os.urandom`) and never reused under the same key — the property that
    makes AES-GCM safe to use at all.
    """
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
    return ":".join(
        (
            _ENVELOPE_VERSION,
            base64.urlsafe_b64encode(nonce).decode("ascii"),
            base64.urlsafe_b64encode(ciphertext).decode("ascii"),
        )
    )


def decrypt(envelope: str, key: bytes) -> bytes:
    """The inverse of `encrypt`. Raises `DecryptionError` — never a bare
    `cryptography`/`binascii`/`ValueError` — for a wrong key, a tampered ciphertext, or
    a malformed envelope alike; callers don't need to distinguish the three.
    """
    try:
        version, nonce_b64, ciphertext_b64 = envelope.split(":")
        if version != _ENVELOPE_VERSION:
            raise ValueError(f"unsupported envelope version {version!r}")
        nonce = base64.urlsafe_b64decode(nonce_b64)
        ciphertext = base64.urlsafe_b64decode(ciphertext_b64)
        return AESGCM(key).decrypt(nonce, ciphertext, None)
    except (ValueError, InvalidTag) as exc:
        raise DecryptionError(
            "failed to decrypt a stored TOTP secret — either the configured "
            "[security.encryption] key doesn't match the one it was encrypted "
            "under, or the stored value is corrupted"
        ) from exc


def ensure_key_configured_if_needed(db: Database, settings: Settings) -> None:
    """Boot-time guard (issue #41 AC): refuses to start if any `totp` credential row
    exists and no encryption key is configured — the `HeartUnavailableError`
    fail-loudly pattern, run from `create_app`'s lifespan (after `run_migrations`,
    since it queries `credentials`) rather than synchronously before uvicorn binds a
    port like Heart/Brain, since it needs the database open first.
    """
    if resolve_key(settings) is not None:
        return

    if CredentialRepository(db).any_credential_of_type(CredentialType.TOTP):
        raise EncryptionKeyMissingError(
            "one or more users hold a TOTP credential but "
            "[security.encryption] key is not configured — set "
            "ANSINA_SECURITY__ENCRYPTION__KEY, or those users can never step up "
            "via TOTP again"
        )
