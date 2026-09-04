"""Unit tests for `ansina.api.authorization.require()`.

Builds a small standalone FastAPI app rather than reusing `ansina.api.app.create_app`
— the real app has no PUT/PATCH/DELETE route to exercise every verb of the fixed
builtin policy against, and this harness lets a test control `request.state.principal`
directly instead of going through a real bearer token per case (that round trip is
`tests/unit/api/test_auth.py`'s job).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from ansina.api.authorization import require
from ansina.api.exception_handlers import ansina_error_handler
from ansina.auth.models import RoleSlug, User, Verb
from ansina.auth.principal import Principal
from ansina.auth.repositories import (
    ResourceRepository,
    RolePermissionRepository,
    RoleRepository,
)
from ansina.errors import AnsinaError
from ansina.storage.database import Database
from ansina.storage.migrator import run_migrations

_RESOURCE = "test.resource"
_SENSITIVE_RESOURCE = "test.sensitive"
_USER = User(
    id="user-1",
    username="tester",
    display_name="",
    active=True,
    created_at="2026-01-01T00:00:00Z",
)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "ansina.db")
    database.connect()
    run_migrations(database)
    yield database
    database.close()


class _StaticPrincipalMiddleware:
    """Test-only stand-in for `BearerAuthMiddleware`: reads an `X-Test-Principal`
    header and looks it up in `principals`, setting `request.state.principal` the same
    way the real middleware does. Real credential-to-`Principal` resolution is `test_
    authenticator.py`'s and `test_auth.py`'s job — this harness only needs `require()`'s
    own decision logic exercised against a `Principal` it already has.
    """

    def __init__(self, app: Any, *, principals: dict[str, Principal]) -> None:
        self._app = app
        self._principals = principals

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", ()))
            key = headers.get(b"x-test-principal")
            if key is not None:
                principal = self._principals.get(key.decode())
                if principal is not None:
                    scope.setdefault("state", {})["principal"] = principal
        await self._app(scope, receive, send)


def _build_app(
    db: Database, principals: dict[str, Principal], *, security_enabled: bool = True
) -> FastAPI:
    app = FastAPI()
    app.state.settings = SimpleNamespace(
        security=SimpleNamespace(enabled=security_enabled)
    )
    app.state.db = db
    app.add_middleware(_StaticPrincipalMiddleware, principals=principals)
    app.add_exception_handler(AnsinaError, ansina_error_handler)

    for method in ("get", "post", "put", "patch", "delete"):
        decorator = getattr(app, method)

        async def _handler() -> dict[str, bool]:
            return {"ok": True}

        decorator(
            "/verb-test",
            dependencies=[Depends(require(_RESOURCE))],
        )(_handler)

    @app.get(
        "/sensitive-test",
        dependencies=[Depends(require(_SENSITIVE_RESOURCE, sensitive=True))],
    )
    async def _sensitive_handler() -> dict[str, bool]:
        return {"ok": True}

    return app


def _grant(
    db: Database, role_slug: RoleSlug, resource: str, verbs: tuple[Verb, ...]
) -> str:
    """Catalog `resource`, grant `role_slug` every verb in `verbs` on it, and return
    the role's real id — `RolePermissionRepository.effective_verbs` matches on that id,
    not on the slug, so a `Principal` built for these tests must carry it.
    """
    ResourceRepository(db).upsert(resource, "")
    role = RoleRepository(db).ensure_builtin(
        role_slug.value, role_slug.value.title(), ""
    )
    for verb in verbs:
        RolePermissionRepository(db).grant(role.id, resource, verb)
    return role.id


def _principal(
    role_ids: frozenset[str],
    role_slugs: frozenset[str],
    *,
    sudo_active: bool = False,
    sudo_grant_id: str | None = None,
) -> Principal:
    return Principal(
        user=_USER,
        role_ids=role_ids,
        role_slugs=role_slugs,
        sudo_active=sudo_active,
        sudo_grant_id=sudo_grant_id,
    )


@pytest.fixture
def client_factory(
    db: Database,
) -> Callable[[dict[str, Principal]], TestClient]:
    def _factory(
        principals: dict[str, Principal], *, security_enabled: bool = True
    ) -> TestClient:
        app = _build_app(db, principals, security_enabled=security_enabled)
        return TestClient(app, raise_server_exceptions=False)

    return _factory


def test_read_role_gets_get_only(db: Database, client_factory: Any) -> None:
    role_id = _grant(db, RoleSlug.READ, _RESOURCE, (Verb.GET,))
    principal = _principal(frozenset({role_id}), frozenset({RoleSlug.READ.value}))
    client = client_factory({"tok": principal})
    headers = {"X-Test-Principal": "tok"}

    assert client.get("/verb-test", headers=headers).status_code == 200
    for method in ("post", "put", "patch", "delete"):
        response = getattr(client, method)("/verb-test", headers=headers)
        assert response.status_code == 403
        assert response.json()["code"] == "ansina.forbidden"


def test_write_role_gets_everything_but_delete(
    db: Database, client_factory: Any
) -> None:
    role_id = _grant(
        db, RoleSlug.WRITE, _RESOURCE, (Verb.GET, Verb.POST, Verb.PUT, Verb.PATCH)
    )
    principal = _principal(frozenset({role_id}), frozenset({RoleSlug.WRITE.value}))
    client = client_factory({"tok": principal})
    headers = {"X-Test-Principal": "tok"}

    for method in ("get", "post", "put", "patch"):
        assert getattr(client, method)("/verb-test", headers=headers).status_code == 200
    assert client.delete("/verb-test", headers=headers).status_code == 403


@pytest.mark.parametrize("role", [RoleSlug.MAINTAIN, RoleSlug.ADMIN])
def test_maintain_and_admin_get_every_verb(
    role: RoleSlug, db: Database, client_factory: Any
) -> None:
    role_id = _grant(db, role, _RESOURCE, tuple(Verb))
    principal = _principal(frozenset({role_id}), frozenset({role.value}))
    client = client_factory({"tok": principal})
    headers = {"X-Test-Principal": "tok"}

    for method in ("get", "post", "put", "patch", "delete"):
        assert getattr(client, method)("/verb-test", headers=headers).status_code == 200


def test_security_disabled_lets_every_request_through_with_no_principal(
    db: Database, client_factory: Any
) -> None:
    client = client_factory({}, security_enabled=False)

    assert client.get("/verb-test").status_code == 200
    assert client.delete("/verb-test").status_code == 200


def test_sensitive_resource_with_admin_never_requires_sudo(
    db: Database, client_factory: Any
) -> None:
    role_id = _grant(db, RoleSlug.ADMIN, _SENSITIVE_RESOURCE, (Verb.GET,))
    principal = _principal(frozenset({role_id}), frozenset({RoleSlug.ADMIN.value}))
    client = client_factory({"tok": principal})

    response = client.get("/sensitive-test", headers={"X-Test-Principal": "tok"})

    assert response.status_code == 200


def test_sensitive_resource_with_maintain_and_no_sudo_grant_is_403(
    db: Database, client_factory: Any
) -> None:
    role_id = _grant(db, RoleSlug.MAINTAIN, _SENSITIVE_RESOURCE, (Verb.GET,))
    principal = _principal(
        frozenset({role_id}), frozenset({RoleSlug.MAINTAIN.value}), sudo_active=False
    )
    client = client_factory({"tok": principal})

    response = client.get("/sensitive-test", headers={"X-Test-Principal": "tok"})

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.auth.sudo_required"


def test_sensitive_resource_with_maintain_and_a_live_sudo_grant_is_200(
    db: Database, client_factory: Any
) -> None:
    role_id = _grant(db, RoleSlug.MAINTAIN, _SENSITIVE_RESOURCE, (Verb.GET,))
    principal = _principal(
        frozenset({role_id}), frozenset({RoleSlug.MAINTAIN.value}), sudo_active=True
    )
    client = client_factory({"tok": principal})

    response = client.get("/sensitive-test", headers={"X-Test-Principal": "tok"})

    assert response.status_code == 200


def test_no_grant_at_all_is_forbidden_not_sudo_required(
    db: Database, client_factory: Any
) -> None:
    ResourceRepository(db).upsert(_SENSITIVE_RESOURCE, "")
    principal = _principal(
        frozenset(), frozenset({RoleSlug.MAINTAIN.value}), sudo_active=False
    )
    client = client_factory({"tok": principal})

    response = client.get("/sensitive-test", headers={"X-Test-Principal": "tok"})

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.forbidden"


def test_authorization_decision_is_audit_logged(
    db: Database,
    client_factory: Any,
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    role_id = _grant(db, RoleSlug.READ, _RESOURCE, (Verb.GET,))
    principal = _principal(frozenset({role_id}), frozenset({RoleSlug.READ.value}))
    client = client_factory({"tok": principal})
    headers = {"X-Test-Principal": "tok"}

    client.get("/verb-test", headers=headers)
    client.post("/verb-test", headers=headers)

    logs = captured_logs()
    granted = [entry for entry in logs if entry["message"] == "authorization granted"]
    denied = [entry for entry in logs if entry["message"] == "authorization denied"]
    assert any(
        entry["extra"]["actor"] == "tester"
        and entry["extra"]["resource"] == _RESOURCE
        and entry["extra"]["verb"] == "GET"
        for entry in granted
    )
    assert any(
        entry["extra"]["actor"] == "tester"
        and entry["extra"]["resource"] == _RESOURCE
        and entry["extra"]["verb"] == "POST"
        and entry["extra"]["code"] == "ansina.forbidden"
        for entry in denied
    )


def test_sudo_grant_id_is_included_in_the_audit_log_line(
    db: Database,
    client_factory: Any,
    captured_logs: Callable[[], list[dict[str, Any]]],
) -> None:
    """Issue #26 AC: "every sensitive action taken under a grant is logged with the
    grant id" — never the grant token itself.
    """
    role_id = _grant(db, RoleSlug.MAINTAIN, _SENSITIVE_RESOURCE, (Verb.GET,))
    principal = _principal(
        frozenset({role_id}),
        frozenset({RoleSlug.MAINTAIN.value}),
        sudo_active=True,
        sudo_grant_id="grant-123",
    )
    client = client_factory({"tok": principal})

    client.get("/sensitive-test", headers={"X-Test-Principal": "tok"})

    logs = captured_logs()
    granted = [entry for entry in logs if entry["message"] == "authorization granted"]
    assert any(entry["extra"].get("sudo_grant_id") == "grant-123" for entry in granted)


async def test_unmapped_verb_is_denied_fail_closed(db: Database) -> None:
    """No real route in this codebase answers a method outside `Verb` — Starlette 405s
    an unregistered method before any dependency runs — so this calls the dependency
    closure directly with a fake `Request` to exercise the fail-closed backstop branch.
    """
    dependency = require(_RESOURCE)
    principal = _principal(frozenset(), frozenset({RoleSlug.ADMIN.value}))
    fake_request = SimpleNamespace(
        method="TRACE",
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(security=SimpleNamespace(enabled=True)),
                db=db,
            )
        ),
        state=SimpleNamespace(principal=principal),
    )

    with pytest.raises(Exception) as excinfo:
        await dependency(fake_request)  # type: ignore[arg-type]

    assert getattr(excinfo.value, "code", None) == "ansina.forbidden"
