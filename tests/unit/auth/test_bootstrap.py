from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ansina.auth.bootstrap import (
    BOOTSTRAP_PROVIDER,
    ensure_bootstrap_admin,
    ensure_configured_admin,
    is_bootstrap_identity,
)
from ansina.auth.models import CredentialType, RoleSlug
from ansina.auth.reconciler import reconcile_builtin_roles
from ansina.auth.repositories import (
    CredentialRepository,
    ExternalIdentityRepository,
    RoleAssignmentRepository,
    UserRepository,
)
from ansina.config import Settings, load_settings
from ansina.logging import get_logger
from ansina.storage.database import Database

# Long enough and high-entropy enough to clear `SecuritySettings.api_token`'s
# strength bar (>=32 chars, base64url charset, >=2.5 bits/char) — see
# `config/settings.py`'s `_TOKEN_MIN_LENGTH`/`_TOKEN_CHARSET`/
# `_TOKEN_MIN_ENTROPY_BITS_PER_CHAR`.
_TOKEN = "configured-admin-token-0123456789a"
_USERNAME = "configured-admin"

# The banner's token line (see `ansina.auth.bootstrap._BANNER`): exactly three
# leading spaces, nothing else on the line.
_BANNER_TOKEN_PATTERN = re.compile(r"^   (\S+)$", re.MULTILINE)


def _settings_with_configured_admin(
    monkeypatch: pytest.MonkeyPatch, *, username: str = _USERNAME, token: str = _TOKEN
) -> Settings:
    monkeypatch.setenv("ANSINA_SECURITY__ADMIN_USERNAME", username)
    monkeypatch.setenv("ANSINA_SECURITY__API_TOKEN", token)
    return load_settings()


def _settings_auto_generate() -> Settings:
    """`security.enabled` at its `True` default, no configured admin — the
    production-default path: only the bootstrap identity is ever provisioned.
    """
    return load_settings()


def _settings_auth_disabled(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("ANSINA_SECURITY__ENABLED", "false")
    return load_settings()


def test_no_op_when_auth_disabled(
    db: Database, clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings_auth_disabled(monkeypatch)

    ensure_bootstrap_admin(db, settings)
    ensure_configured_admin(db, settings)

    assert UserRepository(db).list_all() == []


# --- the bootstrap identity: always auto-generated, never an override, never rotated --


def test_auto_generates_a_token_printed_once_that_authenticates(
    db: Database,
    clean_env: None,
    tmp_cwd: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reconcile_builtin_roles(db)
    settings = _settings_auto_generate()

    ensure_bootstrap_admin(db, settings)

    output = capsys.readouterr().out
    match = _BANNER_TOKEN_PATTERN.search(output)
    assert match is not None, f"bootstrap token banner not found:\n{output}"
    token = match.group(1)

    users = UserRepository(db).list_all()
    assert len(users) == 1
    found = CredentialRepository(db).find_user_by_api_token(token)
    assert found is not None
    assert found.id == users[0].id


def test_auto_generated_token_is_stable_across_restarts(
    db: Database,
    clean_env: None,
    tmp_cwd: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A restart must never silently invalidate a token the operator already copied
    down — issue #28 removed the override path entirely, so this is now the *only*
    behavior: created once, then never touched again by any code path.
    """
    reconcile_builtin_roles(db)
    ensure_bootstrap_admin(db, _settings_auto_generate())
    first_output = capsys.readouterr().out
    first_token = _BANNER_TOKEN_PATTERN.search(first_output)
    assert first_token is not None

    ensure_bootstrap_admin(db, _settings_auto_generate())
    second_output = capsys.readouterr().out

    # Not reprinted on the second boot — it was already shown once, forever.
    assert _BANNER_TOKEN_PATTERN.search(second_output) is None
    assert len(UserRepository(db).list_all()) == 1
    found = CredentialRepository(db).find_user_by_api_token(first_token.group(1))
    assert found is not None


def test_bootstrap_credential_is_never_touched_by_the_configured_admin_env_vars(
    db: Database, clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two mechanisms are fully independent — setting
    `ANSINA_SECURITY__ADMIN_USERNAME`/`API_TOKEN` must never rotate, replace, or
    otherwise touch the bootstrap identity's own credential.
    """
    reconcile_builtin_roles(db)
    ensure_bootstrap_admin(db, _settings_auto_generate())
    bootstrap = ExternalIdentityRepository(db).get_by_provider_subject(
        BOOTSTRAP_PROVIDER, "bootstrap-admin"
    )
    assert bootstrap is not None
    before = CredentialRepository(db).list_api_tokens(bootstrap.user_id)
    assert len(before) == 1

    settings = _settings_with_configured_admin(monkeypatch)
    ensure_bootstrap_admin(db, settings)
    ensure_configured_admin(db, settings)

    after = CredentialRepository(db).list_api_tokens(bootstrap.user_id)
    assert after == before


def test_auto_generated_token_never_appears_unredacted_via_logging(
    db: Database,
    clean_env: None,
    tmp_cwd: Path,
    capsys: pytest.CaptureFixture[str],
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    """The banner bypasses `logging` entirely (see `_print_bootstrap_token_banner`),
    and none of `ensure_bootstrap_admin`'s own log calls include the token — so the
    structured JSON log stream never carries it as things stand. As a defense-in-depth
    backstop for a future call site that accidentally would, the generated token is
    also registered with `logging.redaction` — proven here by deliberately logging it
    and confirming it comes back masked.
    """
    reconcile_builtin_roles(db)
    ensure_bootstrap_admin(db, _settings_auto_generate())

    output = capsys.readouterr().out
    match = _BANNER_TOKEN_PATTERN.search(output)
    assert match is not None
    token = match.group(1)

    get_logger(__name__).info("accidental leak attempt: %s", token)
    logs = captured_logs()
    assert not any(token in str(entry) for entry in logs)


# --- bootstrap_admin_enabled --------------------------------------------------------


def test_bootstrap_admin_enabled_false_revokes_the_credential_but_keeps_the_user(
    db: Database, clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reconcile_builtin_roles(db)
    ensure_bootstrap_admin(db, _settings_auto_generate())
    users_before = UserRepository(db).list_all()
    bootstrap = ExternalIdentityRepository(db).get_by_provider_subject(
        BOOTSTRAP_PROVIDER, "bootstrap-admin"
    )
    assert bootstrap is not None

    monkeypatch.setenv("ANSINA_SECURITY__BOOTSTRAP_ADMIN_ENABLED", "false")
    ensure_bootstrap_admin(db, load_settings())

    assert UserRepository(db).list_all() == users_before
    assert CredentialRepository(db).list_api_tokens(bootstrap.user_id) == []
    identity = ExternalIdentityRepository(db).get_by_provider_subject(
        BOOTSTRAP_PROVIDER, "bootstrap-admin"
    )
    assert identity is not None


def test_bootstrap_admin_enabled_false_with_no_prior_identity_is_a_no_op(
    db: Database, clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reconcile_builtin_roles(db)
    monkeypatch.setenv("ANSINA_SECURITY__BOOTSTRAP_ADMIN_ENABLED", "false")

    ensure_bootstrap_admin(db, load_settings())

    assert UserRepository(db).list_all() == []


def test_re_enabling_regenerates_a_credential_when_currently_credential_less(
    db: Database,
    clean_env: None,
    tmp_cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #28: closes a gap the override path used to paper over — since the
    bootstrap identity is now the *only* break-glass path (no override to fall back
    on), re-enabling it after it was disabled must actually restore access, not leave
    it permanently credential-less.
    """
    reconcile_builtin_roles(db)
    ensure_bootstrap_admin(db, _settings_auto_generate())
    capsys.readouterr()  # discard the first-creation banner
    bootstrap = ExternalIdentityRepository(db).get_by_provider_subject(
        BOOTSTRAP_PROVIDER, "bootstrap-admin"
    )
    assert bootstrap is not None

    monkeypatch.setenv("ANSINA_SECURITY__BOOTSTRAP_ADMIN_ENABLED", "false")
    ensure_bootstrap_admin(db, load_settings())
    assert CredentialRepository(db).list_api_tokens(bootstrap.user_id) == []

    monkeypatch.setenv("ANSINA_SECURITY__BOOTSTRAP_ADMIN_ENABLED", "true")
    ensure_bootstrap_admin(db, load_settings())

    output = capsys.readouterr().out
    match = _BANNER_TOKEN_PATTERN.search(output)
    assert match is not None, f"regeneration banner not found:\n{output}"
    found = CredentialRepository(db).find_user_by_api_token(match.group(1))
    assert found is not None
    assert found.id == bootstrap.user_id


def test_re_enabling_never_touches_a_credential_that_is_still_live(
    db: Database,
    clean_env: None,
    tmp_cwd: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reconcile_builtin_roles(db)
    ensure_bootstrap_admin(db, _settings_auto_generate())
    capsys.readouterr()
    bootstrap = ExternalIdentityRepository(db).get_by_provider_subject(
        BOOTSTRAP_PROVIDER, "bootstrap-admin"
    )
    assert bootstrap is not None
    before = CredentialRepository(db).list_api_tokens(bootstrap.user_id)

    # `bootstrap_admin_enabled` was never disabled — a plain second boot.
    ensure_bootstrap_admin(db, _settings_auto_generate())

    output = capsys.readouterr().out
    assert _BANNER_TOKEN_PATTERN.search(output) is None
    assert CredentialRepository(db).list_api_tokens(bootstrap.user_id) == before


def test_skips_bootstrap_when_a_real_user_already_exists(
    db: Database, clean_env: None, tmp_cwd: Path
) -> None:
    reconcile_builtin_roles(db)
    UserRepository(db).create("alice")

    ensure_bootstrap_admin(db, _settings_auto_generate())

    assert (
        ExternalIdentityRepository(db).get_by_provider_subject(
            BOOTSTRAP_PROVIDER, "bootstrap-admin"
        )
        is None
    )


def test_bootstrap_credential_is_an_api_token_not_a_password(
    db: Database, clean_env: None, tmp_cwd: Path
) -> None:
    reconcile_builtin_roles(db)

    ensure_bootstrap_admin(db, _settings_auto_generate())

    users = UserRepository(db).list_all()
    row = (
        db.connection()
        .execute("SELECT type FROM credentials WHERE user_id = ?", (users[0].id,))
        .fetchone()
    )
    assert row["type"] == CredentialType.API_TOKEN.value


# --- is_bootstrap_identity -----------------------------------------------------------


def test_is_bootstrap_identity_true_for_the_bootstrap_user(
    db: Database, clean_env: None, tmp_cwd: Path
) -> None:
    reconcile_builtin_roles(db)
    ensure_bootstrap_admin(db, _settings_auto_generate())
    bootstrap = ExternalIdentityRepository(db).get_by_provider_subject(
        BOOTSTRAP_PROVIDER, "bootstrap-admin"
    )
    assert bootstrap is not None

    assert is_bootstrap_identity(db, bootstrap.user_id) is True


def test_is_bootstrap_identity_false_for_an_ordinary_user(
    db: Database, clean_env: None, tmp_cwd: Path
) -> None:
    user = UserRepository(db).create("someone-else")

    assert is_bootstrap_identity(db, user.id) is False


def test_is_bootstrap_identity_false_when_no_bootstrap_identity_exists_at_all(
    db: Database,
) -> None:
    assert is_bootstrap_identity(db, "any-id-at-all") is False


# --- the configured admin: env-provisioned, ordinary, first-boot-only ---------------


def test_creates_an_ordinary_admin_from_configured_env_vars(
    db: Database, clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reconcile_builtin_roles(db)
    settings = _settings_with_configured_admin(monkeypatch)

    ensure_configured_admin(db, settings)

    user = UserRepository(db).get_by_username(_USERNAME)
    assert user is not None
    # Ordinary "local" identity — nothing marks this user as special, unlike the
    # bootstrap identity's "local-bootstrap" provider.
    identities = ExternalIdentityRepository(db)
    assert [i.provider for i in identities.list_for_user(user.id)] == ["local"]
    assert is_bootstrap_identity(db, user.id) is False
    roles = RoleAssignmentRepository(db).roles_for_user(user.id)
    assert [r.slug for r in roles] == [RoleSlug.ADMIN.value]
    found = CredentialRepository(db).find_user_by_api_token(_TOKEN)
    assert found is not None
    assert found.id == user.id


def test_no_op_when_admin_username_and_api_token_are_unset(
    db: Database, clean_env: None, tmp_cwd: Path
) -> None:
    reconcile_builtin_roles(db)

    ensure_configured_admin(db, _settings_auto_generate())

    assert UserRepository(db).list_all() == []


def test_fires_correctly_even_though_the_bootstrap_identity_already_exists(
    db: Database, clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for the sequencing gotcha: `ensure_bootstrap_admin` runs first
    in the real boot lifespan and, on a genuine first boot, has already inserted the
    bootstrap identity's own `users` row by the time `ensure_configured_admin` runs.
    Gating on "is `users` empty" would see that row and wrongly conclude it's not the
    first boot, on *every* first boot — the gate must be "no *non-bootstrap* user
    exists yet" instead, which this proves by running the two functions in the same
    order `api.app.create_app`'s lifespan does.
    """
    reconcile_builtin_roles(db)
    settings = _settings_with_configured_admin(monkeypatch)

    ensure_bootstrap_admin(db, settings)  # creates the bootstrap identity first
    ensure_configured_admin(db, settings)

    configured_admin = UserRepository(db).get_by_username(_USERNAME)
    assert configured_admin is not None
    roles = RoleAssignmentRepository(db).roles_for_user(configured_admin.id)
    assert [r.slug for r in roles] == [RoleSlug.ADMIN.value]


def test_second_boot_with_the_same_env_vars_is_a_no_op(
    db: Database, clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A systemd unit or container keeping the same environment across every restart
    — the normal deployment shape — must not fail or duplicate the identity on a
    routine restart.
    """
    reconcile_builtin_roles(db)
    settings = _settings_with_configured_admin(monkeypatch)
    ensure_configured_admin(db, settings)

    ensure_configured_admin(db, settings)

    assert len(UserRepository(db).list_all()) == 1


def test_is_a_no_op_once_any_other_non_bootstrap_user_exists(
    db: Database, clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not specific to the configured admin itself — *any* pre-existing non-bootstrap
    user (e.g. one created via the management API) means this is no longer the first
    boot.
    """
    reconcile_builtin_roles(db)
    UserRepository(db).create("someone-created-earlier")
    settings = _settings_with_configured_admin(monkeypatch)

    ensure_configured_admin(db, settings)

    assert UserRepository(db).get_by_username(_USERNAME) is None


def test_configured_admin_authenticates_immediately(
    db: Database, clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reconcile_builtin_roles(db)
    ensure_configured_admin(db, _settings_with_configured_admin(monkeypatch))

    found = CredentialRepository(db).find_user_by_api_token(_TOKEN)

    assert found is not None
    assert found.active is True
    assert found.deleted_at is None


def test_no_op_when_auth_is_disabled_even_with_env_vars_set(
    db: Database, clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_SECURITY__ENABLED", "false")
    settings = _settings_with_configured_admin(monkeypatch)

    ensure_configured_admin(db, settings)

    assert UserRepository(db).list_all() == []
