"""Unit tests for `ansina.api.tokens` — the shared self-service token surface both
`api.routes.me` and `api.routes.users` build their token routes on. See issue #28.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from ansina.api.tokens import (
    IssuedTokenResponse,
    TokenOut,
    issue_token,
    list_tokens,
    revoke_token,
)
from ansina.auth.bootstrap import ensure_bootstrap_admin
from ansina.auth.management import BootstrapIdentityError, NotFoundError
from ansina.auth.reconciler import reconcile_builtin_roles
from ansina.auth.repositories import CredentialRepository, UserRepository
from ansina.config import load_settings
from ansina.storage.database import Database
from ansina.storage.migrator import run_migrations


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    """A `Database` migrated to the latest schema — same shape as
    `tests/unit/auth/conftest.py`'s own fixture, duplicated here rather than shared
    across the `tests/unit/auth/`/`tests/unit/api/` boundary: `tests/` has no
    `__init__.py` (`pyproject.toml`'s `--import-mode=importlib`), so a cross-directory
    `conftest.py` import isn't reliable — each directory's own fixtures are the unit
    of sharing.
    """
    database = Database(tmp_path / "ansina.db")
    database.connect()
    run_migrations(database)
    yield database
    database.close()


def test_token_out_from_model_never_carries_hash_or_salt(db: Database) -> None:
    user = UserRepository(db).create("alice")
    credential = CredentialRepository(db).create_api_token(
        user.id, "a-real-token", label="laptop"
    )

    out = TokenOut.from_model(credential)

    assert out.id == credential.id
    assert out.label == "laptop"
    assert out.created_at == credential.created_at
    assert out.last_used_at is None
    dumped = out.model_dump()
    assert "hash" not in dumped
    assert "salt" not in dumped


def test_issue_token_mints_a_credential_and_returns_the_raw_value_once(
    db: Database,
) -> None:
    user = UserRepository(db).create("alice")

    issued = issue_token(db, user.id, "laptop")

    assert isinstance(issued, IssuedTokenResponse)
    assert issued.label == "laptop"
    assert issued.token
    found = CredentialRepository(db).find_user_by_api_token(issued.token)
    assert found is not None
    assert found.id == user.id


def test_issue_token_refuses_the_bootstrap_identity(
    db: Database, clean_env: None, tmp_cwd: Path
) -> None:
    reconcile_builtin_roles(db)
    ensure_bootstrap_admin(db, load_settings())
    bootstrap = UserRepository(db).get_by_username("bootstrap-admin")
    assert bootstrap is not None

    with pytest.raises(BootstrapIdentityError):
        issue_token(db, bootstrap.id, "")


def test_list_tokens_returns_both_tokens_metadata(db: Database) -> None:
    """Ordering itself (`CredentialRepository.list_api_tokens`'s `ORDER BY
    created_at, id`) is pinned in `tests/unit/auth/test_repositories.py` — this test
    is only about `list_tokens` projecting every one of a user's tokens correctly.
    """
    user = UserRepository(db).create("alice")
    CredentialRepository(db).create_api_token(user.id, "first", label="a")
    CredentialRepository(db).create_api_token(user.id, "second", label="b")

    listed = list_tokens(db, user.id)

    assert {token.label for token in listed} == {"a", "b"}
    assert all(isinstance(token, TokenOut) for token in listed)


def test_list_tokens_is_empty_for_a_user_with_no_tokens(db: Database) -> None:
    user = UserRepository(db).create("alice")

    assert list_tokens(db, user.id) == []


def test_revoke_token_deletes_it_and_it_no_longer_authenticates(db: Database) -> None:
    user = UserRepository(db).create("alice")
    credential = CredentialRepository(db).create_api_token(user.id, "a-token")

    revoke_token(db, user.id, credential.id)

    assert CredentialRepository(db).find_user_by_api_token("a-token") is None


def test_revoke_token_404s_for_an_unknown_token_id(db: Database) -> None:
    user = UserRepository(db).create("alice")

    with pytest.raises(NotFoundError):
        revoke_token(db, user.id, "no-such-token-id")


def test_revoke_token_404s_for_a_token_belonging_to_a_different_user(
    db: Database,
) -> None:
    owner = UserRepository(db).create("owner")
    other = UserRepository(db).create("other")
    credential = CredentialRepository(db).create_api_token(owner.id, "owned-token")

    with pytest.raises(NotFoundError):
        revoke_token(db, other.id, credential.id)
    # The token is untouched — the 404 didn't accidentally delete it.
    assert CredentialRepository(db).find_user_by_api_token("owned-token") is not None


def test_revoke_token_refuses_the_bootstrap_identity(
    db: Database, clean_env: None, tmp_cwd: Path
) -> None:
    reconcile_builtin_roles(db)
    ensure_bootstrap_admin(db, load_settings())
    bootstrap = UserRepository(db).get_by_username("bootstrap-admin")
    assert bootstrap is not None
    token = CredentialRepository(db).list_api_tokens(bootstrap.id)[0]

    with pytest.raises(BootstrapIdentityError):
        revoke_token(db, bootstrap.id, token.id)
