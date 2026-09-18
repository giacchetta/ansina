from __future__ import annotations

import base64
from pathlib import Path

import pytest

from ansina.auth.encryption import (
    DecryptionError,
    EncryptionKeyMissingError,
    decrypt,
    encrypt,
    ensure_key_configured_if_needed,
    resolve_key,
)
from ansina.auth.repositories import CredentialRepository, UserRepository
from ansina.config import load_settings
from ansina.storage.database import Database

_KEY = base64.urlsafe_b64decode("inl1_UnlPfMEYIPwFZnl46Nx2GXZmHoHdT-OC4I9nYA" + "=" * 1)
_OTHER_KEY = base64.urlsafe_b64decode(
    "z_r6b_46vTMTZ0OU4gYRnDf0X_2rQO0chQrIw8_zzz8" + "=" * 1
)


def test_encrypt_round_trips_through_decrypt() -> None:
    plaintext = b"a totp secret, 20 raw bytes!"

    envelope = encrypt(plaintext, _KEY)
    decrypted = decrypt(envelope, _KEY)

    assert decrypted == plaintext


def test_envelope_is_versioned_and_never_the_plaintext() -> None:
    plaintext = b"super secret totp seed"

    envelope = encrypt(plaintext, _KEY)

    assert envelope.startswith("v1:")
    assert plaintext not in envelope.encode("ascii", errors="ignore")
    assert len(envelope.split(":")) == 3


def test_encrypt_never_reuses_a_nonce() -> None:
    plaintext = b"same plaintext twice"

    first = encrypt(plaintext, _KEY)
    second = encrypt(plaintext, _KEY)

    assert first != second


def test_decrypt_with_the_wrong_key_fails() -> None:
    envelope = encrypt(b"secret", _KEY)

    with pytest.raises(DecryptionError):
        decrypt(envelope, _OTHER_KEY)


def test_decrypt_rejects_a_tampered_ciphertext() -> None:
    envelope = encrypt(b"secret", _KEY)
    version, nonce_b64, ciphertext_b64 = envelope.split(":")
    tampered = f"{version}:{nonce_b64}:{ciphertext_b64[:-4]}AAAA"

    with pytest.raises(DecryptionError):
        decrypt(tampered, _KEY)


def test_decrypt_rejects_an_unknown_version() -> None:
    envelope = encrypt(b"secret", _KEY)
    _version, nonce_b64, ciphertext_b64 = envelope.split(":")
    bad_version = f"v2:{nonce_b64}:{ciphertext_b64}"

    with pytest.raises(DecryptionError):
        decrypt(bad_version, _KEY)


def test_decrypt_rejects_a_malformed_envelope() -> None:
    with pytest.raises(DecryptionError):
        decrypt("not-even-close-to-an-envelope", _KEY)


def test_resolve_key_returns_none_when_unset(clean_env: None, tmp_cwd: Path) -> None:
    settings = load_settings()

    assert resolve_key(settings) is None


def test_resolve_key_decodes_the_configured_value(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "ANSINA_SECURITY__ENCRYPTION__KEY",
        "inl1_UnlPfMEYIPwFZnl46Nx2GXZmHoHdT-OC4I9nYA",
    )
    settings = load_settings()

    key = resolve_key(settings)

    assert key == _KEY


def test_ensure_key_configured_is_a_no_op_when_a_key_is_set(
    db: Database, clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "ANSINA_SECURITY__ENCRYPTION__KEY",
        "inl1_UnlPfMEYIPwFZnl46Nx2GXZmHoHdT-OC4I9nYA",
    )
    settings = load_settings()

    ensure_key_configured_if_needed(db, settings)  # must not raise


def test_ensure_key_configured_is_a_no_op_with_no_totp_credential(
    db: Database, clean_env: None, tmp_cwd: Path
) -> None:
    settings = load_settings()

    ensure_key_configured_if_needed(db, settings)  # must not raise


def test_ensure_key_configured_refuses_to_boot_with_an_orphaned_totp_credential(
    db: Database, clean_env: None, tmp_cwd: Path
) -> None:
    user = UserRepository(db).create("alice")
    CredentialRepository(db).create_totp_secret(user.id, "v1:nonce:ciphertext")
    settings = load_settings()

    with pytest.raises(EncryptionKeyMissingError, match="ANSINA_SECURITY__ENCRYPTION"):
        ensure_key_configured_if_needed(db, settings)
