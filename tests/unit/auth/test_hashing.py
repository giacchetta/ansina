from __future__ import annotations

from pathlib import Path

from ansina.auth.hashing import (
    Argon2Params,
    dummy_password_hash,
    hash_password,
    hash_token,
    new_token_salt,
    password_needs_rehash,
    verify_password,
    verify_token_hash,
)
from ansina.config import load_settings


def test_hash_password_returns_a_phc_string_not_the_raw_password(
    cheap_argon2: Argon2Params,
) -> None:
    stored = hash_password("correct horse battery staple", cheap_argon2)

    assert stored.startswith("$argon2id$")
    assert "correct horse battery staple" not in stored


def test_verify_password_round_trips(cheap_argon2: Argon2Params) -> None:
    stored = hash_password("hunter2", cheap_argon2)

    assert verify_password("hunter2", stored, cheap_argon2) is True


def test_verify_password_rejects_a_wrong_password(cheap_argon2: Argon2Params) -> None:
    stored = hash_password("hunter2", cheap_argon2)

    assert verify_password("wrong", stored, cheap_argon2) is False


def test_verify_password_rejects_a_malformed_hash(cheap_argon2: Argon2Params) -> None:
    assert verify_password("hunter2", "not-a-valid-hash", cheap_argon2) is False


def test_password_needs_rehash_is_false_under_the_same_params(
    cheap_argon2: Argon2Params,
) -> None:
    stored = hash_password("hunter2", cheap_argon2)

    assert password_needs_rehash(stored, cheap_argon2) is False


def test_password_needs_rehash_is_true_under_stronger_params(
    cheap_argon2: Argon2Params,
) -> None:
    stored = hash_password("hunter2", cheap_argon2)
    stronger = Argon2Params(time_cost=2, memory_cost_kib=16, parallelism=1)

    assert password_needs_rehash(stored, stronger) is True


def test_argon2_params_from_settings_reads_security_password(
    clean_env: None, tmp_cwd: Path
) -> None:
    settings = load_settings()

    params = Argon2Params.from_settings(settings)

    assert params.time_cost == settings.security.password.time_cost
    assert params.memory_cost_kib == settings.security.password.memory_cost_kib
    assert params.parallelism == settings.security.password.parallelism


def test_new_token_salt_is_unique_per_call() -> None:
    assert new_token_salt() != new_token_salt()


def test_hash_token_is_deterministic_given_the_same_salt() -> None:
    salt = new_token_salt()

    assert hash_token("a-token", salt) == hash_token("a-token", salt)


def test_hash_token_differs_across_salts() -> None:
    token = "a-token"

    assert hash_token(token, new_token_salt()) != hash_token(token, new_token_salt())


def test_verify_token_hash_round_trips() -> None:
    salt = new_token_salt()
    stored = hash_token("a-real-token", salt)

    assert verify_token_hash("a-real-token", salt, stored) is True


def test_verify_token_hash_rejects_a_wrong_token() -> None:
    salt = new_token_salt()
    stored = hash_token("a-real-token", salt)

    assert verify_token_hash("a-wrong-token", salt, stored) is False


# --- dummy_password_hash: issue #50's timing-equalization primitive ----------------


def test_dummy_password_hash_is_a_real_phc_string(cheap_argon2: Argon2Params) -> None:
    stored = dummy_password_hash(cheap_argon2)

    assert stored.startswith("$argon2id$")


def test_dummy_password_hash_never_verifies_against_any_submitted_password(
    cheap_argon2: Argon2Params,
) -> None:
    stored = dummy_password_hash(cheap_argon2)

    assert verify_password("hunter2", stored, cheap_argon2) is False
    assert verify_password("", stored, cheap_argon2) is False


def test_dummy_password_hash_is_cached_per_params(cheap_argon2: Argon2Params) -> None:
    """Same `params` -> the identical hash string, not a fresh one each call — the
    whole point is to avoid paying a second argon2 hash generation per request.
    """
    assert dummy_password_hash(cheap_argon2) == dummy_password_hash(cheap_argon2)


def test_dummy_password_hash_differs_across_params() -> None:
    """A different `Argon2Params` gets its own cache entry — a dummy generated under
    one work factor wouldn't equalize a verify run under another.
    """
    other = Argon2Params(time_cost=2, memory_cost_kib=16, parallelism=1)

    assert dummy_password_hash(Argon2Params(1, 8, 1)) != dummy_password_hash(other)
