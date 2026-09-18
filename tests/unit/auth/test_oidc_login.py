"""Tests for `ansina.auth.oidc_login.OidcLoginService`. See issue #43.

Drives the full login exchange (`start_login`/`complete_login`) against a real
migrated `Database` and a fake IdP (`fake_idp_client_factory`,
`tests/conftest.py`) — no network. `test_oidc.py` already covers every individual
ID-token rejection case in isolation; these tests focus on state handling,
provisioning/linking, and the per-login role-mapping refresh (AC #4), plus proving
those rejections still block provisioning when wired end to end through this service.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from ansina.auth.bootstrap import ensure_bootstrap_admin
from ansina.auth.clock import iso
from ansina.auth.models import SubjectType
from ansina.auth.oidc import OidcHttpClient, OidcTokenError
from ansina.auth.oidc_login import (
    OidcLoginService,
    OidcProvisioningError,
    OidcStateError,
    build_oidc_login_service,
)
from ansina.auth.reconciler import reconcile_builtin_roles
from ansina.auth.repositories import (
    ExternalIdentityRepository,
    OidcLoginStateRepository,
    RoleAssignmentRepository,
    RoleMappingRepository,
    RoleRepository,
    UserRepository,
)
from ansina.config import load_settings
from ansina.config.settings import OidcSettings
from ansina.storage.database import Database

FakeClientFactory = Callable[..., OidcHttpClient]

_START = datetime(2026, 1, 1, tzinfo=UTC)


class _Clock:
    """An injectable, manually-advanced clock — never real sleeping. Same shape
    `test_sudo.py`'s own `_Clock` already uses.
    """

    def __init__(self, start: datetime = _START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def oidc_settings(oidc_issuer: str, oidc_client_id: str) -> OidcSettings:
    return OidcSettings(
        enabled=True,
        issuer=oidc_issuer,
        client_id=oidc_client_id,
        client_secret=SecretStr("test-client-secret"),
        redirect_uri="https://ansina.test/auth/oidc/callback",
        token_ttl_seconds=3600.0,
        state_ttl_seconds=600.0,
        http_timeout_seconds=5.0,
    )


@pytest.fixture
def service(
    db: Database,
    oidc_settings: OidcSettings,
    fake_idp_client_factory: FakeClientFactory,
    idp_jwks: dict[str, object],
    clock: _Clock,
    sign_id_token: Callable[..., str],
) -> OidcLoginService:
    """A service wired to a fake IdP that answers with a fully valid, fresh token by
    default — `complete_login` in most tests here just needs to get past validation
    to exercise the provisioning/role-sync logic that's actually under test.
    """
    client = fake_idp_client_factory(
        issuer=oidc_settings.issuer,
        jwks=idp_jwks,
        token_response={"id_token": sign_id_token()},
    )
    return OidcLoginService(db, oidc_settings, client=client, clock=clock)


def _start_and_reconfigure_token(
    service: OidcLoginService,
    fake_idp_client_factory: FakeClientFactory,
    oidc_settings: OidcSettings,
    idp_jwks: dict[str, object],
    sign_id_token: Callable[..., str],
    **token_kwargs: object,
) -> tuple[str, str]:
    """`start_login`, then swap the service's client for one whose token endpoint
    returns an ID token signed with this login's own real `nonce` (`start_login`
    generates one fresh each call, so a test can't hardcode it) plus whatever
    `token_kwargs` it wants to override. Returns `(code, state)` for `complete_login`.
    """
    start = service.start_login()
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(start.authorization_url).query)
    nonce = query["nonce"][0]
    token_kwargs.setdefault("nonce", nonce)
    new_client = fake_idp_client_factory(
        issuer=oidc_settings.issuer,
        jwks=idp_jwks,
        token_response={"id_token": sign_id_token(**token_kwargs)},
    )
    service._client = new_client
    return "auth-code", start.state


# --- start_login ----------------------------------------------------------------------


def test_start_login_builds_a_pkce_authorization_url(
    service: OidcLoginService, oidc_settings: OidcSettings
) -> None:
    from urllib.parse import parse_qs, urlparse

    start = service.start_login()

    parsed = urlparse(start.authorization_url)
    assert parsed.geturl().startswith(f"{oidc_settings.issuer}/authorize?")
    query = parse_qs(parsed.query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [oidc_settings.client_id]
    assert query["redirect_uri"] == [oidc_settings.redirect_uri]
    assert query["scope"] == ["openid profile email"]
    assert query["state"] == [start.state]
    assert "nonce" in query
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["code_challenge"][0]) > 0


def test_start_login_persists_state_with_the_configured_ttl(
    db: Database, service: OidcLoginService, oidc_settings: OidcSettings, clock: _Clock
) -> None:
    start = service.start_login()

    stored = OidcLoginStateRepository(db).take(start.state, now=iso(clock.now))
    assert stored is not None
    assert stored.expires_at == start.expires_at
    expected_expiry = clock.now + timedelta(seconds=oidc_settings.state_ttl_seconds)
    assert stored.expires_at == iso(expected_expiry)


def test_start_login_sweeps_previously_expired_states(
    db: Database, service: OidcLoginService, clock: _Clock
) -> None:
    stale = service.start_login()
    clock.advance(3601.0)  # past the 600s state_ttl_seconds

    service.start_login()  # triggers delete_expired as a side effect

    assert OidcLoginStateRepository(db).take(stale.state, now=iso(clock.now)) is None


# --- complete_login: state handling -----------------------------------------------


def test_complete_login_rejects_an_unknown_state(service: OidcLoginService) -> None:
    with pytest.raises(OidcStateError):
        service.complete_login("some-code", "never-issued-state")


def test_complete_login_rejects_a_replayed_state(
    service: OidcLoginService,
    fake_idp_client_factory: FakeClientFactory,
    oidc_settings: OidcSettings,
    idp_jwks: dict[str, object],
    sign_id_token: Callable[..., str],
) -> None:
    code, state = _start_and_reconfigure_token(
        service, fake_idp_client_factory, oidc_settings, idp_jwks, sign_id_token
    )
    service.complete_login(code, state)

    with pytest.raises(OidcStateError):
        service.complete_login(code, state)


def test_complete_login_rejects_an_expired_state(
    service: OidcLoginService,
    fake_idp_client_factory: FakeClientFactory,
    oidc_settings: OidcSettings,
    idp_jwks: dict[str, object],
    sign_id_token: Callable[..., str],
    clock: _Clock,
) -> None:
    code, state = _start_and_reconfigure_token(
        service, fake_idp_client_factory, oidc_settings, idp_jwks, sign_id_token
    )
    clock.advance(oidc_settings.state_ttl_seconds + 1)

    with pytest.raises(OidcStateError):
        service.complete_login(code, state)


def test_complete_login_a_forged_token_never_touches_the_database(
    db: Database,
    service: OidcLoginService,
    fake_idp_client_factory: FakeClientFactory,
    oidc_settings: OidcSettings,
    idp_jwks: dict[str, object],
    sign_id_token: Callable[..., str],
) -> None:
    """AC #2, wired end to end: a validation failure (here, a wrong audience) must
    reject before step 4 (provisioning) ever runs — no `users`/`external_identities`
    row appears.
    """
    code, state = _start_and_reconfigure_token(
        service,
        fake_idp_client_factory,
        oidc_settings,
        idp_jwks,
        sign_id_token,
        audience="not-the-configured-client-id",
    )

    with pytest.raises(OidcTokenError):
        service.complete_login(code, state)

    assert UserRepository(db).list_all() == []


# --- complete_login: provisioning ---------------------------------------------------


def test_complete_login_provisions_a_new_user_from_preferred_username(
    db: Database,
    service: OidcLoginService,
    fake_idp_client_factory: FakeClientFactory,
    oidc_settings: OidcSettings,
    idp_jwks: dict[str, object],
    sign_id_token: Callable[..., str],
) -> None:
    code, state = _start_and_reconfigure_token(
        service,
        fake_idp_client_factory,
        oidc_settings,
        idp_jwks,
        sign_id_token,
        sub="subject-1",
        extra_claims={"preferred_username": "alice", "email": "alice@example.com"},
    )

    user = service.complete_login(code, state)

    assert user.username == "alice"
    identity = ExternalIdentityRepository(db).get_by_provider_subject(
        oidc_settings.issuer, "subject-1"
    )
    assert identity is not None
    assert identity.user_id == user.id


def test_complete_login_falls_back_to_email_then_sub_for_username(
    service: OidcLoginService,
    fake_idp_client_factory: FakeClientFactory,
    oidc_settings: OidcSettings,
    idp_jwks: dict[str, object],
    sign_id_token: Callable[..., str],
) -> None:
    code, state = _start_and_reconfigure_token(
        service,
        fake_idp_client_factory,
        oidc_settings,
        idp_jwks,
        sign_id_token,
        sub="subject-email-fallback",
        extra_claims={"email": "bob@example.com"},
    )

    user = service.complete_login(code, state)

    assert user.username == "bob@example.com"


def test_complete_login_falls_back_to_sub_when_no_other_claim(
    service: OidcLoginService,
    fake_idp_client_factory: FakeClientFactory,
    oidc_settings: OidcSettings,
    idp_jwks: dict[str, object],
    sign_id_token: Callable[..., str],
) -> None:
    code, state = _start_and_reconfigure_token(
        service,
        fake_idp_client_factory,
        oidc_settings,
        idp_jwks,
        sign_id_token,
        sub="bare-sub-999",
    )

    user = service.complete_login(code, state)

    assert user.username == "bare-sub-999"


def test_complete_login_reuses_the_user_on_a_second_login_for_the_same_subject(
    db: Database,
    service: OidcLoginService,
    fake_idp_client_factory: FakeClientFactory,
    oidc_settings: OidcSettings,
    idp_jwks: dict[str, object],
    sign_id_token: Callable[..., str],
) -> None:
    code1, state1 = _start_and_reconfigure_token(
        service,
        fake_idp_client_factory,
        oidc_settings,
        idp_jwks,
        sign_id_token,
        sub="stable-subject",
        extra_claims={"preferred_username": "carol"},
    )
    first_user = service.complete_login(code1, state1)

    code2, state2 = _start_and_reconfigure_token(
        service,
        fake_idp_client_factory,
        oidc_settings,
        idp_jwks,
        sign_id_token,
        sub="stable-subject",
        extra_claims={"preferred_username": "carol"},
    )
    second_user = service.complete_login(code2, state2)

    assert first_user.id == second_user.id
    assert len(UserRepository(db).list_all()) == 1


def test_complete_login_refuses_a_tombstoned_linked_user(
    db: Database,
    service: OidcLoginService,
    fake_idp_client_factory: FakeClientFactory,
    oidc_settings: OidcSettings,
    idp_jwks: dict[str, object],
    sign_id_token: Callable[..., str],
    clock: _Clock,
) -> None:
    code1, state1 = _start_and_reconfigure_token(
        service,
        fake_idp_client_factory,
        oidc_settings,
        idp_jwks,
        sign_id_token,
        sub="to-be-deleted",
        extra_claims={"preferred_username": "dave"},
    )
    user = service.complete_login(code1, state1)
    UserRepository(db).soft_delete(user.id, deleted_at=iso(clock.now))

    code2, state2 = _start_and_reconfigure_token(
        service,
        fake_idp_client_factory,
        oidc_settings,
        idp_jwks,
        sign_id_token,
        sub="to-be-deleted",
        extra_claims={"preferred_username": "dave"},
    )
    with pytest.raises(OidcProvisioningError):
        service.complete_login(code2, state2)


def test_complete_login_refuses_a_deactivated_linked_user(
    db: Database,
    service: OidcLoginService,
    fake_idp_client_factory: FakeClientFactory,
    oidc_settings: OidcSettings,
    idp_jwks: dict[str, object],
    sign_id_token: Callable[..., str],
) -> None:
    code1, state1 = _start_and_reconfigure_token(
        service,
        fake_idp_client_factory,
        oidc_settings,
        idp_jwks,
        sign_id_token,
        sub="to-be-deactivated",
        extra_claims={"preferred_username": "erin"},
    )
    user = service.complete_login(code1, state1)
    UserRepository(db).set_active(user.id, active=False)

    code2, state2 = _start_and_reconfigure_token(
        service,
        fake_idp_client_factory,
        oidc_settings,
        idp_jwks,
        sign_id_token,
        sub="to-be-deactivated",
        extra_claims={"preferred_username": "erin"},
    )
    with pytest.raises(OidcProvisioningError):
        service.complete_login(code2, state2)


# --- complete_login: linking onto an existing local username ------------------------


def test_complete_login_links_onto_an_existing_local_user_with_matching_username(
    db: Database,
    service: OidcLoginService,
    fake_idp_client_factory: FakeClientFactory,
    oidc_settings: OidcSettings,
    idp_jwks: dict[str, object],
    sign_id_token: Callable[..., str],
) -> None:
    existing = UserRepository(db).create("preexisting-local-user")

    code, state = _start_and_reconfigure_token(
        service,
        fake_idp_client_factory,
        oidc_settings,
        idp_jwks,
        sign_id_token,
        sub="new-idp-subject",
        extra_claims={"preferred_username": "preexisting-local-user"},
    )
    user = service.complete_login(code, state)

    assert user.id == existing.id
    identity = ExternalIdentityRepository(db).get_by_provider_subject(
        oidc_settings.issuer, "new-idp-subject"
    )
    assert identity is not None
    assert identity.user_id == existing.id


def test_complete_login_refuses_to_link_onto_a_tombstoned_username(
    db: Database,
    service: OidcLoginService,
    fake_idp_client_factory: FakeClientFactory,
    oidc_settings: OidcSettings,
    idp_jwks: dict[str, object],
    sign_id_token: Callable[..., str],
    clock: _Clock,
) -> None:
    existing = UserRepository(db).create("deleted-local-user")
    UserRepository(db).soft_delete(existing.id, deleted_at=iso(clock.now))

    code, state = _start_and_reconfigure_token(
        service,
        fake_idp_client_factory,
        oidc_settings,
        idp_jwks,
        sign_id_token,
        sub="new-idp-subject-2",
        extra_claims={"preferred_username": "deleted-local-user"},
    )

    with pytest.raises(OidcProvisioningError):
        service.complete_login(code, state)


def test_complete_login_refuses_to_link_onto_a_deactivated_username(
    db: Database,
    service: OidcLoginService,
    fake_idp_client_factory: FakeClientFactory,
    oidc_settings: OidcSettings,
    idp_jwks: dict[str, object],
    sign_id_token: Callable[..., str],
) -> None:
    existing = UserRepository(db).create("inactive-local-user")
    UserRepository(db).set_active(existing.id, active=False)

    code, state = _start_and_reconfigure_token(
        service,
        fake_idp_client_factory,
        oidc_settings,
        idp_jwks,
        sign_id_token,
        sub="new-idp-subject-3",
        extra_claims={"preferred_username": "inactive-local-user"},
    )

    with pytest.raises(OidcProvisioningError):
        service.complete_login(code, state)


def test_complete_login_refuses_to_link_onto_the_bootstrap_identity(
    db: Database,
    service: OidcLoginService,
    fake_idp_client_factory: FakeClientFactory,
    oidc_settings: OidcSettings,
    idp_jwks: dict[str, object],
    sign_id_token: Callable[..., str],
    clean_env: None,
    tmp_cwd: Path,
) -> None:
    reconcile_builtin_roles(db)
    ensure_bootstrap_admin(db, load_settings())
    bootstrap = UserRepository(db).get_by_username("bootstrap-admin")
    assert bootstrap is not None

    code, state = _start_and_reconfigure_token(
        service,
        fake_idp_client_factory,
        oidc_settings,
        idp_jwks,
        sign_id_token,
        sub="attempted-bootstrap-link",
        extra_claims={"preferred_username": "bootstrap-admin"},
    )

    with pytest.raises(OidcProvisioningError):
        service.complete_login(code, state)


# --- complete_login: role-mapping refresh on every login (AC #4) --------------------


def test_complete_login_applies_role_mappings_on_first_login(
    db: Database,
    service: OidcLoginService,
    fake_idp_client_factory: FakeClientFactory,
    oidc_settings: OidcSettings,
    idp_jwks: dict[str, object],
    sign_id_token: Callable[..., str],
) -> None:
    role = RoleRepository(db).create("ops", "Ops", "")
    RoleMappingRepository(db).create(
        oidc_settings.issuer, "groups", "ops-team", role.id
    )

    code, state = _start_and_reconfigure_token(
        service,
        fake_idp_client_factory,
        oidc_settings,
        idp_jwks,
        sign_id_token,
        sub="mapped-user",
        extra_claims={"groups": ["ops-team"]},
    )
    user = service.complete_login(code, state)

    assert [r.id for r in RoleAssignmentRepository(db).roles_for_user(user.id)] == [
        role.id
    ]


def test_complete_login_revokes_a_mapped_role_when_claims_change(
    db: Database,
    service: OidcLoginService,
    fake_idp_client_factory: FakeClientFactory,
    oidc_settings: OidcSettings,
    idp_jwks: dict[str, object],
    sign_id_token: Callable[..., str],
) -> None:
    """AC #4, the headline scenario: a role granted on first login is revoked on a
    second login whose claims no longer match the mapping — without disturbing a
    manually (`local`) assigned role on the same user.
    """
    roles = RoleRepository(db)
    mapped_role = roles.create("ops", "Ops", "")
    local_role = roles.create("legacy", "Legacy", "")
    RoleMappingRepository(db).create(
        oidc_settings.issuer, "groups", "ops-team", mapped_role.id
    )

    code1, state1 = _start_and_reconfigure_token(
        service,
        fake_idp_client_factory,
        oidc_settings,
        idp_jwks,
        sign_id_token,
        sub="claims-change-user",
        extra_claims={"groups": ["ops-team"]},
    )
    user = service.complete_login(code1, state1)
    RoleAssignmentRepository(db).assign(
        SubjectType.USER, user.id, local_role.id, source="local"
    )

    code2, state2 = _start_and_reconfigure_token(
        service,
        fake_idp_client_factory,
        oidc_settings,
        idp_jwks,
        sign_id_token,
        sub="claims-change-user",
        extra_claims={"groups": ["some-other-group"]},
    )
    service.complete_login(code2, state2)

    assert [r.id for r in RoleAssignmentRepository(db).roles_for_user(user.id)] == [
        local_role.id
    ]


# --- clock / token_ttl_seconds properties -------------------------------------------


def test_clock_and_token_ttl_seconds_properties(
    db: Database, oidc_settings: OidcSettings, clock: _Clock
) -> None:
    class _NullClient:
        def get_json(self, url: str) -> dict[str, object]:  # pragma: no cover
            raise AssertionError("not exercised in this test")

        def post_form(
            self, url: str, form: object, *, auth: object
        ) -> dict[str, object]:  # pragma: no cover
            raise AssertionError("not exercised in this test")

    service = OidcLoginService(db, oidc_settings, client=_NullClient(), clock=clock)

    assert service.clock is clock
    assert service.token_ttl_seconds == oidc_settings.token_ttl_seconds


# --- build_oidc_login_service ---------------------------------------------------------


def test_build_returns_none_when_disabled(
    db: Database, clean_env: None, tmp_cwd: Path
) -> None:
    settings = load_settings()
    assert settings.security.oidc.enabled is False

    assert build_oidc_login_service(db, settings) is None


def test_build_returns_a_service_when_enabled(
    db: Database,
    oidc_settings: OidcSettings,
    clean_env: None,
    tmp_cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANSINA_SECURITY__OIDC__ENABLED", "true")
    monkeypatch.setenv("ANSINA_SECURITY__OIDC__ISSUER", oidc_settings.issuer)
    monkeypatch.setenv("ANSINA_SECURITY__OIDC__CLIENT_ID", oidc_settings.client_id)
    monkeypatch.setenv("ANSINA_SECURITY__OIDC__CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv(
        "ANSINA_SECURITY__OIDC__REDIRECT_URI", oidc_settings.redirect_uri
    )
    settings = load_settings()

    service = build_oidc_login_service(db, settings)

    assert isinstance(service, OidcLoginService)
    assert service.token_ttl_seconds == oidc_settings.token_ttl_seconds
