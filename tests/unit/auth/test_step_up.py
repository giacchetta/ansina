from __future__ import annotations

from pathlib import Path

import pytest

from ansina.auth.hashing import Argon2Params
from ansina.auth.models import User
from ansina.auth.principal import Principal
from ansina.auth.repositories import CredentialRepository, UserRepository
from ansina.auth.step_up import (
    PasswordStepUpVerifier,
    StepUpRegistry,
    build_step_up_verifiers,
)
from ansina.config import load_settings
from ansina.storage.database import Database


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


def test_build_step_up_verifiers_returns_one_password_verifier(
    db: Database, clean_env: None, tmp_cwd: Path
) -> None:
    settings = load_settings()

    verifiers = build_step_up_verifiers(db, settings)

    assert len(verifiers) == 1
    assert verifiers[0].name == "password"
