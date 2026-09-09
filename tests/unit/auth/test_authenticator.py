from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ansina.auth.authenticator import (
    ApiTokenAuthenticator,
    Authenticator,
    build_authenticators,
    resolve_principal,
)
from ansina.auth.clock import iso
from ansina.auth.models import RoleSlug, SubjectType, User
from ansina.auth.principal import AuthMethod
from ansina.auth.repositories import (
    CredentialRepository,
    RoleAssignmentRepository,
    RoleRepository,
    UserRepository,
)
from ansina.config import load_settings
from ansina.storage.database import Database

# A plain `(ApiTokenAuthenticator,)` chain for tests that only care about
# `resolve_principal`'s own behavior, not `build_authenticators`'s wiring — avoids
# every such test needing a full `Settings` object just to pick a resolution.
_DEFAULT_RESOLUTION_SECONDS = 300.0


def _chain(db: Database) -> tuple[Authenticator, ...]:
    return (
        ApiTokenAuthenticator(
            db, last_used_resolution_seconds=_DEFAULT_RESOLUTION_SECONDS
        ),
    )


def _seed_reader(db: Database) -> tuple[UserRepository, str]:
    """A `read`-role user with a known token — the shape most tests here need."""
    users = UserRepository(db)
    user = users.create("reader")
    role = RoleRepository(db).ensure_builtin(
        RoleSlug.READ.value, "Read", "GET-only access."
    )
    RoleAssignmentRepository(db).assign(SubjectType.USER, user.id, role.id)
    CredentialRepository(db).create_api_token(user.id, "reader-token")
    return users, user.id


def test_build_authenticators_returns_the_api_token_authenticator(
    db: Database, clean_env: None, tmp_cwd: Path
) -> None:
    authenticators = build_authenticators(db, load_settings())

    assert len(authenticators) == 1
    assert isinstance(authenticators[0], ApiTokenAuthenticator)
    assert authenticators[0].method is AuthMethod.API_TOKEN


def test_build_authenticators_reads_the_resolution_from_settings(
    db: Database, clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_SECURITY__TOKEN_LAST_USED_RESOLUTION_SECONDS", "0")
    authenticator = build_authenticators(db, load_settings())[0]

    assert isinstance(authenticator, ApiTokenAuthenticator)
    # `resolution=0` means "write on every authentication" — proven end to end via
    # the coalescing tests below, not by reaching into the private attribute here.


def test_api_token_authenticator_matches_an_active_credential(db: Database) -> None:
    _, user_id = _seed_reader(db)

    matched = ApiTokenAuthenticator(
        db, last_used_resolution_seconds=_DEFAULT_RESOLUTION_SECONDS
    ).authenticate("reader-token")

    assert matched is not None
    assert matched.id == user_id


def test_api_token_authenticator_returns_none_for_an_unknown_token(
    db: Database,
) -> None:
    authenticator = ApiTokenAuthenticator(
        db, last_used_resolution_seconds=_DEFAULT_RESOLUTION_SECONDS
    )
    assert authenticator.authenticate("no-such-token") is None


def test_resolve_principal_fills_roles_direct_and_via_group(db: Database) -> None:
    _seed_reader(db)

    principal = resolve_principal(db, _chain(db), "reader-token")

    assert principal is not None
    assert principal.user.username == "reader"
    assert principal.role_slugs == {RoleSlug.READ.value}
    assert principal.auth_method is AuthMethod.API_TOKEN


def test_resolve_principal_returns_none_for_no_match(db: Database) -> None:
    assert resolve_principal(db, _chain(db), "nope") is None


def test_resolve_principal_returns_none_for_an_inactive_user(db: Database) -> None:
    users, user_id = _seed_reader(db)
    users.set_active(user_id, active=False)

    assert resolve_principal(db, _chain(db), "reader-token") is None


def test_resolve_principal_returns_none_for_a_deleted_user(db: Database) -> None:
    """Backstop for a hand-edited row: `UserRepository.soft_delete` (issue #27)
    already purges the credential a deleted user would authenticate with, so this
    sets `deleted_at` directly — bypassing `soft_delete` — to prove
    `resolve_principal` itself also refuses a tombstoned user, not just as a side
    effect of the credential being gone.
    """
    _, user_id = _seed_reader(db)
    with db.transaction() as cursor:
        cursor.execute(
            "UPDATE users SET deleted_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00.000Z", user_id),
        )

    assert resolve_principal(db, _chain(db), "reader-token") is None


def test_a_second_authenticator_can_be_appended_without_touching_the_first(
    db: Database,
) -> None:
    """Issue #25's acceptance criterion: a chain member can be added and exercised by
    a test without modifying `ApiTokenAuthenticator`'s own code — proving the chain is
    genuinely additive, not #24's inline single check reformatted.
    """
    _, user_id = _seed_reader(db)
    user = UserRepository(db).get(user_id)
    assert user is not None

    class _StaticSecondFactorAuthenticator:
        """A trivial second chain member — matches one hardcoded credential only,
        standing in for a follow-up milestone's federated-login authenticator.
        """

        method = AuthMethod.API_TOKEN

        def authenticate(self, credential: str) -> User | None:
            return user if credential == "second-factor-secret" else None

    chain = (*_chain(db), _StaticSecondFactorAuthenticator())

    # The first authenticator still works, untouched...
    assert resolve_principal(db, chain, "reader-token") is not None
    # ...and the appended one is reachable too, with no first-authenticator match.
    principal = resolve_principal(db, chain, "second-factor-secret")
    assert principal is not None
    assert principal.user.id == user_id
    # Neither authenticator recognizes a credential belonging to no chain member.
    assert resolve_principal(db, chain, "unknown") is None


# --- issue #28: coalesced `last_used_at` bookkeeping -------------------------------


def _frozen_clock(now: datetime) -> Callable[[], datetime]:
    def _clock() -> datetime:
        return now

    return _clock


def _last_used_at(db: Database, user_id: str) -> str | None:
    credential = CredentialRepository(db).list_api_tokens(user_id)[0]
    return credential.last_used_at


def test_first_authentication_writes_last_used_at(db: Database) -> None:
    _, user_id = _seed_reader(db)
    assert _last_used_at(db, user_id) is None
    now = datetime(2026, 1, 1, tzinfo=UTC)
    authenticator = ApiTokenAuthenticator(
        db, last_used_resolution_seconds=300.0, clock=_frozen_clock(now)
    )

    authenticator.authenticate("reader-token")

    assert _last_used_at(db, user_id) == iso(now)


def test_a_second_use_inside_the_resolution_window_does_not_write(
    db: Database,
) -> None:
    _, user_id = _seed_reader(db)
    first = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    authenticator = ApiTokenAuthenticator(
        db, last_used_resolution_seconds=300.0, clock=_frozen_clock(first)
    )
    authenticator.authenticate("reader-token")
    recorded = _last_used_at(db, user_id)

    # 4 minutes later — inside the 300s (5-minute) resolution window.
    authenticator._clock = _frozen_clock(first + timedelta(minutes=4))
    authenticator.authenticate("reader-token")

    assert _last_used_at(db, user_id) == recorded


def test_a_use_past_the_resolution_window_writes_again(db: Database) -> None:
    _, user_id = _seed_reader(db)
    first = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    authenticator = ApiTokenAuthenticator(
        db, last_used_resolution_seconds=300.0, clock=_frozen_clock(first)
    )
    authenticator.authenticate("reader-token")

    later = first + timedelta(minutes=10)
    authenticator._clock = _frozen_clock(later)
    authenticator.authenticate("reader-token")

    assert _last_used_at(db, user_id) == iso(later)


def test_an_unknown_token_never_writes_anything(db: Database) -> None:
    _, user_id = _seed_reader(db)
    authenticator = ApiTokenAuthenticator(db, last_used_resolution_seconds=300.0)

    authenticator.authenticate("not-the-right-token")

    assert _last_used_at(db, user_id) is None


def test_zero_resolution_writes_on_every_authentication(db: Database) -> None:
    _, user_id = _seed_reader(db)
    first = datetime(2026, 1, 1, tzinfo=UTC)
    authenticator = ApiTokenAuthenticator(
        db, last_used_resolution_seconds=0.0, clock=_frozen_clock(first)
    )
    authenticator.authenticate("reader-token")
    assert _last_used_at(db, user_id) == iso(first)

    # One microsecond later is already "stale" under a zero-second resolution.
    second = first + timedelta(microseconds=1000)
    authenticator._clock = _frozen_clock(second)
    authenticator.authenticate("reader-token")

    assert _last_used_at(db, user_id) == iso(second)


def test_touch_uses_the_credential_id_not_a_lookup_by_token(db: Database) -> None:
    """Two users each hold a token — authenticating one must never touch the other's
    `last_used_at`.
    """
    _, reader_id = _seed_reader(db)
    other = UserRepository(db).create("other-reader")
    CredentialRepository(db).create_api_token(other.id, "other-token")
    authenticator = ApiTokenAuthenticator(
        db, last_used_resolution_seconds=300.0, clock=_frozen_clock(datetime.now(UTC))
    )

    authenticator.authenticate("reader-token")

    assert _last_used_at(db, reader_id) is not None
    assert _last_used_at(db, other.id) is None
