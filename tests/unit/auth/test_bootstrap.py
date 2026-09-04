from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ansina.auth.bootstrap import BOOTSTRAP_PROVIDER, ensure_bootstrap_admin
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
_TOKEN = "bootstrap-test-token-0123456789ab"

# The banner's token line (see `ansina.auth.bootstrap._BANNER`): exactly three
# leading spaces, nothing else on the line.
_BANNER_TOKEN_PATTERN = re.compile(r"^   (\S+)$", re.MULTILINE)


def _settings_with_override(
    monkeypatch: pytest.MonkeyPatch, token: str = _TOKEN
) -> Settings:
    monkeypatch.setenv("ANSINA_SECURITY__API_TOKEN", token)
    return load_settings()


def _settings_auto_generate() -> Settings:
    """`security.enabled` at its `True` default, no override configured — the
    production-default path.
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

    assert UserRepository(db).list_all() == []


# --- auto-generated bootstrap token (no override configured) -----------------------


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
    down — unlike the operator-override path, the auto-generated credential is never
    re-synced/rotated once created.
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
    also registered with `logging.redaction` (unlike the banner, an operator-supplied
    override is already registered by `logging.setup.configure_logging`) — proven
    here by deliberately logging it and confirming it comes back masked.
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


# --- operator-supplied override token ------------------------------------------------


def test_creates_a_synthetic_admin_with_an_override_token(
    db: Database, clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reconcile_builtin_roles(db)
    settings = _settings_with_override(monkeypatch)

    ensure_bootstrap_admin(db, settings)

    users = UserRepository(db).list_all()
    assert len(users) == 1
    identities = ExternalIdentityRepository(db)
    identity = identities.get_by_provider_subject(BOOTSTRAP_PROVIDER, "bootstrap-admin")
    assert identity is not None
    assert identity.user_id == users[0].id
    # No "local" row too — the bootstrap identity uses "local-bootstrap" *instead of*
    # "local" (it authenticates via api_token, not a password-login-shaped account).
    assert [i.provider for i in identities.list_for_user(users[0].id)] == [
        BOOTSTRAP_PROVIDER
    ]
    roles = RoleAssignmentRepository(db).roles_for_user(users[0].id)
    assert [r.slug for r in roles] == [RoleSlug.ADMIN.value]
    found = CredentialRepository(db).find_user_by_api_token(_TOKEN)
    assert found is not None
    assert found.id == users[0].id


def test_second_boot_with_the_same_override_does_not_duplicate_the_identity(
    db: Database, clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reconcile_builtin_roles(db)
    settings = _settings_with_override(monkeypatch)
    ensure_bootstrap_admin(db, settings)

    ensure_bootstrap_admin(db, settings)

    assert len(UserRepository(db).list_all()) == 1


def test_rotating_the_override_token_updates_the_existing_credential(
    db: Database, clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reconcile_builtin_roles(db)
    old_settings = _settings_with_override(
        monkeypatch, "old-token-0123456789abcdefghijklmn"
    )
    ensure_bootstrap_admin(db, old_settings)

    new_settings = _settings_with_override(
        monkeypatch, "new-token-0123456789abcdefghijklmn"
    )
    ensure_bootstrap_admin(db, new_settings)

    credentials = CredentialRepository(db)
    assert (
        credentials.find_user_by_api_token("old-token-0123456789abcdefghijklmn") is None
    )
    assert (
        credentials.find_user_by_api_token("new-token-0123456789abcdefghijklmn")
        is not None
    )


# --- bootstrap_admin_enabled ----------------------------------------------------------


def test_bootstrap_admin_enabled_false_revokes_the_credential_but_keeps_the_user(
    db: Database, clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reconcile_builtin_roles(db)
    ensure_bootstrap_admin(db, _settings_with_override(monkeypatch))
    users_before = UserRepository(db).list_all()

    monkeypatch.setenv("ANSINA_SECURITY__BOOTSTRAP_ADMIN_ENABLED", "false")
    disabled_settings = _settings_with_override(monkeypatch)
    ensure_bootstrap_admin(db, disabled_settings)

    assert UserRepository(db).list_all() == users_before
    assert CredentialRepository(db).find_user_by_api_token(_TOKEN) is None
    identity = ExternalIdentityRepository(db).get_by_provider_subject(
        BOOTSTRAP_PROVIDER, "bootstrap-admin"
    )
    assert identity is not None


def test_bootstrap_admin_enabled_false_with_no_prior_identity_is_a_no_op(
    db: Database, clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reconcile_builtin_roles(db)
    monkeypatch.setenv("ANSINA_SECURITY__BOOTSTRAP_ADMIN_ENABLED", "false")
    settings = _settings_with_override(monkeypatch)

    ensure_bootstrap_admin(db, settings)

    assert UserRepository(db).list_all() == []


def test_skips_bootstrap_when_a_real_user_already_exists(
    db: Database, clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reconcile_builtin_roles(db)
    UserRepository(db).create("alice")

    ensure_bootstrap_admin(db, _settings_with_override(monkeypatch))

    assert (
        ExternalIdentityRepository(db).get_by_provider_subject(
            BOOTSTRAP_PROVIDER, "bootstrap-admin"
        )
        is None
    )


def test_bootstrap_credential_is_an_api_token_not_a_password(
    db: Database, clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reconcile_builtin_roles(db)
    settings = _settings_with_override(monkeypatch)

    ensure_bootstrap_admin(db, settings)

    users = UserRepository(db).list_all()
    row = (
        db.connection()
        .execute("SELECT type FROM credentials WHERE user_id = ?", (users[0].id,))
        .fetchone()
    )
    assert row["type"] == CredentialType.API_TOKEN.value
