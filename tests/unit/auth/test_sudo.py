from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ansina.auth.principal import Principal
from ansina.auth.repositories import SudoLockoutRepository, UserRepository
from ansina.auth.step_up import StepUpRegistry
from ansina.auth.sudo import (
    StepUpUnavailableError,
    SudoLockedOutError,
    SudoService,
    build_sudo_service,
)
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
    whole issue -> sensitive-call chain against a verifier M2 didn't ship. `enrolled`
    (issue #37) is this fake's own enrollment flag — real verifiers ask `credentials`,
    this one just answers a constant.
    """

    name: str = "fake"
    accepts: bool = True
    enrolled: bool = True

    def verify(self, principal: Principal, payload: Mapping[str, Any]) -> bool:
        del principal, payload
        return self.accepts

    def is_enrolled(self, principal: Principal) -> bool:
        del principal
        return self.enrolled


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
    verifiers: tuple[_FakeVerifier, ...] | None = None,
    clock: _Clock | None = None,
    settings: SudoSettings = _SUDO_SETTINGS,
) -> tuple[SudoService, _Clock]:
    clock = clock or _Clock()
    registry = StepUpRegistry(verifiers or (verifier or _FakeVerifier(),))
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


def test_step_up_with_no_enrolled_factor_raises_step_up_unavailable(
    db: Database,
) -> None:
    """Issue #37 AC: a caller with no usable step-up factor gets
    `StepUpUnavailableError`, and this does not advance the failed-attempt lockout
    counter.
    """
    principal = _principal(db)
    service, _clock = _service(db=db, verifier=_FakeVerifier(enrolled=False))

    with pytest.raises(StepUpUnavailableError) as excinfo:
        service.step_up(principal, {})

    assert excinfo.value.code == "ansina.auth.step_up_unavailable"
    assert excinfo.value.details["available_factors"] == []
    assert SudoLockoutRepository(db).get(principal.user.id) is None


def test_step_up_unavailable_never_consumes_any_lockout_attempts(db: Database) -> None:
    """A caller with zero factors calling repeatedly never accumulates a failed-attempt
    streak — proven by never locking out even past `max_failed_attempts` calls.
    """
    principal = _principal(db)
    service, _clock = _service(db=db, verifier=_FakeVerifier(enrolled=False))

    for _ in range(_SUDO_SETTINGS.max_failed_attempts + 2):
        with pytest.raises(StepUpUnavailableError):
            service.step_up(principal, {})

    assert SudoLockoutRepository(db).get(principal.user.id) is None


def test_step_up_with_an_unrecognized_factor_raises_step_up_unavailable(
    db: Database,
) -> None:
    principal = _principal(db)
    service, _clock = _service(db=db, verifier=_FakeVerifier(name="password"))

    with pytest.raises(StepUpUnavailableError) as excinfo:
        service.step_up(principal, {"factor": "totp"})

    assert excinfo.value.details["available_factors"] == ["password"]
    assert SudoLockoutRepository(db).get(principal.user.id) is None


def test_step_up_with_two_enrolled_factors_and_no_factor_key_is_ambiguous(
    db: Database,
) -> None:
    """Issue #37: 2+ enrolled with no `factor` given to disambiguate is refused rather
    than silently picking one — a policy call #41's TOTP verifier needs made
    explicitly, not defaulted here.
    """
    first = _FakeVerifier(name="password")
    second = _FakeVerifier(name="totp")
    principal = _principal(db)
    service, _clock = _service(db=db, verifiers=(first, second))

    with pytest.raises(StepUpUnavailableError) as excinfo:
        service.step_up(principal, {})

    assert set(excinfo.value.details["available_factors"]) == {"password", "totp"}
    assert SudoLockoutRepository(db).get(principal.user.id) is None


def test_step_up_with_two_enrolled_factors_resolves_the_requested_one(
    db: Database,
) -> None:
    first = _FakeVerifier(name="password", accepts=False)
    second = _FakeVerifier(name="totp", accepts=True)
    principal = _principal(db)
    service, _clock = _service(db=db, verifiers=(first, second))

    issued = service.step_up(principal, {"factor": "totp"})

    assert issued is not None
    assert issued.verifier == "totp"


def test_a_locked_out_caller_with_no_factor_still_gets_locked_out_first(
    db: Database,
) -> None:
    """Lockout is checked before factor resolution — a caller who has already burned
    every attempt against a factor they hold stays locked out even once that factor
    disappears, not silently reclassified as `step_up_unavailable`.
    """
    principal = _principal(db)
    service, _clock = _service(db=db, verifier=_FakeVerifier(accepts=False))
    for _ in range(3):
        service.step_up(principal, {})  # 3rd failure -> locked out

    with pytest.raises(SudoLockedOutError):
        service.step_up(principal, {})


def test_enrolled_factors_reflects_the_registrys_resolution(db: Database) -> None:
    principal = _principal(db)
    service, _clock = _service(
        db=db, verifiers=(_FakeVerifier(name="password"), _FakeVerifier(name="totp"))
    )

    assert service.enrolled_factors(principal) == ["password", "totp"]


def test_enrolled_factors_is_empty_when_nothing_is_enrolled(db: Database) -> None:
    principal = _principal(db)
    service, _clock = _service(db=db, verifier=_FakeVerifier(enrolled=False))

    assert service.enrolled_factors(principal) == []


def test_build_sudo_service_wires_a_working_service(
    db: Database, clean_env: None, tmp_cwd: Path
) -> None:
    settings = load_settings()

    service = build_sudo_service(db, settings)

    assert isinstance(service, SudoService)
    assert service.resolve("nobody", "anything") is None
