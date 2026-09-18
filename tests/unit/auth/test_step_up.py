from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ansina.auth.encryption import encrypt
from ansina.auth.hashing import Argon2Params
from ansina.auth.models import User
from ansina.auth.principal import Principal
from ansina.auth.repositories import CredentialRepository, UserRepository
from ansina.auth.step_up import (
    PasswordStepUpVerifier,
    StepUpRegistry,
    TotpStepUpVerifier,
    build_step_up_verifiers,
)
from ansina.auth.totp import totp_code
from ansina.config import load_settings
from ansina.storage.database import Database

_KEY = b"0" * 32
_TOTP_SECRET = b"1" * 20


def _principal(user: User) -> Principal:
    return Principal(user=user, role_ids=frozenset())


def test_password_verifier_accepts_the_right_password(
    db: Database, cheap_argon2: Argon2Params
) -> None:
    user = UserRepository(db).create("alice")
    CredentialRepository(db).set_password(user.id, "hunter2", cheap_argon2)
    verifier = PasswordStepUpVerifier(db, cheap_argon2)

    assert verifier.verify(_principal(user), {"password": "hunter2"}) is True
    assert verifier.name == "password"


def test_password_verifier_rejects_the_wrong_password(
    db: Database, cheap_argon2: Argon2Params
) -> None:
    user = UserRepository(db).create("alice")
    CredentialRepository(db).set_password(user.id, "hunter2", cheap_argon2)
    verifier = PasswordStepUpVerifier(db, cheap_argon2)

    assert verifier.verify(_principal(user), {"password": "wrong"}) is False


@pytest.mark.parametrize("payload", [{}, {"password": None}, {"password": 123}])
def test_password_verifier_treats_a_malformed_payload_as_a_non_match(
    db: Database, cheap_argon2: Argon2Params, payload: dict[str, object]
) -> None:
    user = UserRepository(db).create("alice")
    CredentialRepository(db).set_password(user.id, "hunter2", cheap_argon2)
    verifier = PasswordStepUpVerifier(db, cheap_argon2)

    assert verifier.verify(_principal(user), payload) is False


def test_password_verifier_is_enrolled_once_a_password_is_set(
    db: Database, cheap_argon2: Argon2Params
) -> None:
    user = UserRepository(db).create("alice")
    verifier = PasswordStepUpVerifier(db, cheap_argon2)
    assert verifier.is_enrolled(_principal(user)) is False

    CredentialRepository(db).set_password(user.id, "hunter2", cheap_argon2)

    assert verifier.is_enrolled(_principal(user)) is True


class _FakeVerifier:
    """A minimal second `StepUpVerifier` for exercising `StepUpRegistry` filtering and
    ordering without touching `credentials`.
    """

    def __init__(self, name: str, *, enrolled: bool) -> None:
        self._name = name
        self._enrolled = enrolled

    @property
    def name(self) -> str:
        return self._name

    def verify(self, principal: Principal, payload: object) -> bool:
        del principal, payload
        return True

    def is_enrolled(self, principal: Principal) -> bool:
        del principal
        return self._enrolled


def test_registry_for_principal_returns_only_enrolled_verifiers_in_order(
    db: Database,
) -> None:
    user = UserRepository(db).create("alice")
    a = _FakeVerifier("a", enrolled=True)
    b = _FakeVerifier("b", enrolled=False)
    c = _FakeVerifier("c", enrolled=True)
    registry = StepUpRegistry((a, b, c))

    resolved = registry.for_principal(_principal(user))

    assert resolved == (a, c)


def test_registry_for_principal_returns_empty_tuple_when_nothing_is_enrolled(
    db: Database,
) -> None:
    user = UserRepository(db).create("alice")
    registry = StepUpRegistry((_FakeVerifier("a", enrolled=False),))

    assert registry.for_principal(_principal(user)) == ()


def test_registry_refuses_to_construct_with_no_verifiers() -> None:
    with pytest.raises(ValueError, match="StepUpVerifier"):
        StepUpRegistry(())


def test_build_step_up_verifiers_returns_password_and_totp(
    db: Database, clean_env: None, tmp_cwd: Path
) -> None:
    settings = load_settings()

    verifiers = build_step_up_verifiers(db, settings)

    assert [v.name for v in verifiers] == ["password", "totp"]


# --- TotpStepUpVerifier (issue #41) --------------------------------------------------


def _fixed_clock(when: datetime) -> Callable[[], datetime]:
    return lambda: when


def test_totp_verifier_accepts_the_current_code(db: Database) -> None:
    user = UserRepository(db).create("alice")
    CredentialRepository(db).create_totp_secret(user.id, encrypt(_TOTP_SECRET, _KEY))
    now = datetime(2024, 1, 1, tzinfo=UTC)
    verifier = TotpStepUpVerifier(db, _KEY, clock=_fixed_clock(now))
    code = totp_code(_TOTP_SECRET, at=int(now.timestamp()))

    assert verifier.verify(_principal(user), {"code": code}) is True
    assert verifier.name == "totp"


def test_totp_verifier_rejects_a_wrong_code(db: Database) -> None:
    user = UserRepository(db).create("alice")
    CredentialRepository(db).create_totp_secret(user.id, encrypt(_TOTP_SECRET, _KEY))
    now = datetime(2024, 1, 1, tzinfo=UTC)
    verifier = TotpStepUpVerifier(db, _KEY, clock=_fixed_clock(now))

    assert verifier.verify(_principal(user), {"code": "000000"}) is False


def test_totp_verifier_rejects_a_replayed_code(db: Database) -> None:
    user = UserRepository(db).create("alice")
    CredentialRepository(db).create_totp_secret(user.id, encrypt(_TOTP_SECRET, _KEY))
    now = datetime(2024, 1, 1, tzinfo=UTC)
    verifier = TotpStepUpVerifier(db, _KEY, clock=_fixed_clock(now))
    code = totp_code(_TOTP_SECRET, at=int(now.timestamp()))

    assert verifier.verify(_principal(user), {"code": code}) is True
    assert verifier.verify(_principal(user), {"code": code}) is False


def test_totp_verifier_accepts_a_later_code_after_a_replay_attempt(
    db: Database,
) -> None:
    user = UserRepository(db).create("alice")
    CredentialRepository(db).create_totp_secret(user.id, encrypt(_TOTP_SECRET, _KEY))
    now = datetime(2024, 1, 1, tzinfo=UTC)
    verifier = TotpStepUpVerifier(db, _KEY, clock=_fixed_clock(now))
    first_code = totp_code(_TOTP_SECRET, at=int(now.timestamp()))
    assert verifier.verify(_principal(user), {"code": first_code}) is True

    later = now + timedelta(seconds=30)
    later_verifier = TotpStepUpVerifier(db, _KEY, clock=_fixed_clock(later))
    later_code = totp_code(_TOTP_SECRET, at=int(later.timestamp()))

    assert later_verifier.verify(_principal(user), {"code": later_code}) is True


@pytest.mark.parametrize("payload", [{}, {"code": None}, {"code": 123456}])
def test_totp_verifier_treats_a_malformed_payload_as_a_non_match(
    db: Database, payload: dict[str, object]
) -> None:
    user = UserRepository(db).create("alice")
    CredentialRepository(db).create_totp_secret(user.id, encrypt(_TOTP_SECRET, _KEY))
    verifier = TotpStepUpVerifier(db, _KEY)

    assert verifier.verify(_principal(user), payload) is False


def test_totp_verifier_rejects_when_not_enrolled(db: Database) -> None:
    user = UserRepository(db).create("alice")
    verifier = TotpStepUpVerifier(db, _KEY)

    assert verifier.verify(_principal(user), {"code": "123456"}) is False


def test_totp_verifier_rejects_with_no_key_configured(db: Database) -> None:
    user = UserRepository(db).create("alice")
    CredentialRepository(db).create_totp_secret(user.id, encrypt(_TOTP_SECRET, _KEY))
    verifier = TotpStepUpVerifier(db, None)

    assert verifier.verify(_principal(user), {"code": "123456"}) is False


def test_totp_verifier_rejects_when_the_stored_envelope_fails_to_decrypt(
    db: Database,
) -> None:
    user = UserRepository(db).create("alice")
    CredentialRepository(db).create_totp_secret(user.id, encrypt(_TOTP_SECRET, _KEY))
    wrong_key = b"9" * 32
    verifier = TotpStepUpVerifier(db, wrong_key)

    assert verifier.verify(_principal(user), {"code": "123456"}) is False


def test_totp_verifier_is_enrolled_once_a_secret_is_stored(db: Database) -> None:
    user = UserRepository(db).create("alice")
    verifier = TotpStepUpVerifier(db, _KEY)
    assert verifier.is_enrolled(_principal(user)) is False

    CredentialRepository(db).create_totp_secret(user.id, encrypt(_TOTP_SECRET, _KEY))

    assert verifier.is_enrolled(_principal(user)) is True
