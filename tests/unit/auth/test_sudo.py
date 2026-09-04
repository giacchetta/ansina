from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ansina.auth.principal import Principal
from ansina.auth.repositories import UserRepository
from ansina.auth.step_up import StepUpRegistry
from ansina.auth.sudo import SudoLockedOutError, SudoService, build_sudo_service
from ansina.config import load_settings
from ansina.config.settings import SudoSettings
from ansina.storage.database import Database

_START = datetime(2026, 1, 1, tzinfo=UTC)


class _Clock:
    """An injectable, manually-advanced clock — never real sleeping."""

    def __init__(self, start: datetime = _START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class _FakeVerifier:
    """A second `StepUpVerifier` implementation that never touches
    `PasswordStepUpVerifier` or a `credentials` row — this is the pluggability AC:
    `SudoService` never names a concrete verifier, so swapping this in exercises the
    whole issue -> sensitive-call chain against a verifier M2 didn't ship.
    """

    name: str = "fake"
    accepts: bool = True

    def verify(self, principal: Principal, payload: Mapping[str, Any]) -> bool:
        del principal, payload
        return self.accepts


_SUDO_SETTINGS = SudoSettings(
    ttl_seconds=600.0,
    max_failed_attempts=3,
    attempt_window_seconds=300.0,
    lockout_seconds=900.0,
)


def _principal(db: Database, username: str = "alice") -> Principal:
    user = UserRepository(db).create(username)
    return Principal(user=user, role_ids=frozenset())


def _service(
    *,
    db: Database,
    verifier: _FakeVerifier | None = None,
    clock: _Clock | None = None,
    settings: SudoSettings = _SUDO_SETTINGS,
) -> tuple[SudoService, _Clock]:
    clock = clock or _Clock()
    registry = StepUpRegistry((verifier or _FakeVerifier(),))
    return SudoService(db, settings, registry=registry, clock=clock), clock


def test_successful_step_up_issues_a_grant_resolve_can_find(db: Database) -> None:
    principal = _principal(db)
    service, _clock = _service(db=db)

    issued = service.step_up(principal, {})

    assert issued is not None
    assert issued.verifier == "fake"
    resolved = service.resolve(principal.user.id, issued.token)
    assert resolved is not None
    assert resolved.id == issued.grant_id


def test_failed_step_up_returns_none_and_issues_no_grant(db: Database) -> None:
    principal = _principal(db)
    service, _clock = _service(db=db, verifier=_FakeVerifier(accepts=False))

    assert service.step_up(principal, {}) is None
    assert service.resolve(principal.user.id, "anything") is None


def test_resolve_of_a_wrong_token_returns_none(db: Database) -> None:
    principal = _principal(db)
    service, _clock = _service(db=db)
    issued = service.step_up(principal, {})
    assert issued is not None

    assert service.resolve(principal.user.id, "not-the-real-token") is None


def test_a_grant_expires_after_its_ttl(db: Database) -> None:
    principal = _principal(db)
    clock = _Clock()
    settings = SudoSettings(
        ttl_seconds=60.0,
        max_failed_attempts=3,
        attempt_window_seconds=300.0,
        lockout_seconds=900.0,
    )
    service, _clock = _service(db=db, clock=clock, settings=settings)
    issued = service.step_up(principal, {})
    assert issued is not None

    clock.advance(59.0)
    assert service.resolve(principal.user.id, issued.token) is not None

    clock.advance(2.0)  # now 61s later — past the 60s TTL
    assert service.resolve(principal.user.id, issued.token) is None


def test_revoke_for_user_invalidates_the_grant(db: Database) -> None:
    principal = _principal(db)
    service, _clock = _service(db=db)
    issued = service.step_up(principal, {})
    assert issued is not None

    service.revoke_for_user(principal.user.id)

    assert service.resolve(principal.user.id, issued.token) is None


def test_revoke_all_invalidates_every_users_grant(db: Database) -> None:
    alice = _principal(db, "alice")
    bob = _principal(db, "bob")
    service, _clock = _service(db=db)
    alice_grant = service.step_up(alice, {})
    bob_grant = service.step_up(bob, {})
    assert alice_grant is not None
    assert bob_grant is not None

    service.revoke_all()

    assert service.resolve(alice.user.id, alice_grant.token) is None
    assert service.resolve(bob.user.id, bob_grant.token) is None


def test_a_successful_step_up_replaces_an_earlier_grant(db: Database) -> None:
    """`SudoGrantRepository.create` deletes a user's prior grant — a re-step-up
    replaces, never accumulates.
    """
    principal = _principal(db)
    service, _clock = _service(db=db)
    first = service.step_up(principal, {})
    assert first is not None

    second = service.step_up(principal, {})
    assert second is not None

    assert service.resolve(principal.user.id, first.token) is None
    assert service.resolve(principal.user.id, second.token) is not None


def test_lockout_after_max_failed_attempts_refuses_further_attempts(
    db: Database,
) -> None:
    principal = _principal(db)
    service, _clock = _service(db=db, verifier=_FakeVerifier(accepts=False))

    assert service.step_up(principal, {}) is None
    assert service.step_up(principal, {}) is None
    assert service.step_up(principal, {}) is None  # 3rd failure -> locked out

    with pytest.raises(SudoLockedOutError) as excinfo:
        service.step_up(principal, {})
    assert excinfo.value.details["retry_after_seconds"] == pytest.approx(900.0)


def test_lockout_does_not_consume_a_verifier_call(db: Database) -> None:
    """While locked out, `step_up` never even asks the verifier — proven by an
    accepting verifier still being refused.
    """
    principal = _principal(db)
    failing = _FakeVerifier(accepts=False)
    service, _clock = _service(db=db, verifier=failing)
    for _ in range(3):
        service.step_up(principal, {})

    failing.accepts = True  # would succeed now, if it were ever asked
    with pytest.raises(SudoLockedOutError):
        service.step_up(principal, {})


def test_lockout_clears_once_the_cooldown_elapses(db: Database) -> None:
    principal = _principal(db)
    service, clock = _service(db=db, verifier=_FakeVerifier(accepts=False))
    for _ in range(3):
        service.step_up(principal, {})

    clock.advance(_SUDO_SETTINGS.lockout_seconds + 1.0)

    # No longer locked out — a real verification attempt runs and can still fail.
    assert service.step_up(principal, {}) is None


def test_a_failure_outside_the_attempt_window_resets_the_streak(db: Database) -> None:
    principal = _principal(db)
    service, clock = _service(db=db, verifier=_FakeVerifier(accepts=False))
    service.step_up(principal, {})
    service.step_up(principal, {})

    clock.advance(_SUDO_SETTINGS.attempt_window_seconds + 1.0)
    service.step_up(principal, {})  # streak reset to 1, not the 3rd strike

    # Still not locked out — two more failures are needed to reach the threshold
    # again from a freshly reset streak.
    assert service.step_up(principal, {}) is None


def test_a_successful_step_up_clears_a_partial_failure_streak(db: Database) -> None:
    principal = _principal(db)
    failing = _FakeVerifier(accepts=False)
    service, _clock = _service(db=db, verifier=failing)
    service.step_up(principal, {})  # one failure, below the 3-attempt threshold

    failing.accepts = True
    issued = service.step_up(principal, {})
    assert issued is not None

    # The earlier failure was cleared, not carried forward — two more failures are
    # needed to reach the lockout threshold again, not just one.
    failing.accepts = False
    assert service.step_up(principal, {}) is None
    assert service.step_up(principal, {}) is None
    resolved_before_lockout = service.resolve(principal.user.id, issued.token)
    # The grant issued above is still independently valid — a failed re-step-up
    # doesn't revoke an already-live grant.
    assert resolved_before_lockout is not None


def test_build_sudo_service_wires_a_working_service(
    db: Database, clean_env: None, tmp_cwd: Path
) -> None:
    settings = load_settings()

    service = build_sudo_service(db, settings)

    assert isinstance(service, SudoService)
    assert service.resolve("nobody", "anything") is None
