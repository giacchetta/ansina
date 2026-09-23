from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from ansina.api.cors import (
    ALLOW_HEADERS,
    ALLOW_METHODS,
    EXPOSE_HEADERS,
    add_cors_middleware,
)
from ansina.auth.models import Verb
from ansina.config import load_settings


def test_allow_methods_covers_every_verb() -> None:
    """`ALLOW_METHODS` is derived from `Verb` so it can never drift from what
    `role_permissions` can actually grant — plus OPTIONS, the preflight method
    itself, which is never a grantable verb.
    """
    assert set(ALLOW_METHODS) == {*(verb.value for verb in Verb), "OPTIONS"}


def test_allow_headers_carries_authorization_and_sudo_token() -> None:
    assert "Authorization" in ALLOW_HEADERS
    assert "X-Sudo-Token" in ALLOW_HEADERS


def test_expose_headers_carries_request_id_and_retry_after() -> None:
    assert "X-Request-ID" in EXPOSE_HEADERS
    assert "Retry-After" in EXPOSE_HEADERS


def test_add_cors_middleware_noop_when_disabled(clean_env: None, tmp_cwd: Path) -> None:
    settings = load_settings()
    app = FastAPI()

    added = add_cors_middleware(app, settings)

    assert added is False
    assert app.user_middleware == []


def test_add_cors_middleware_adds_when_enabled(
    clean_env: None, tmp_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_SECURITY__CORS__ENABLED", "true")
    monkeypatch.setenv(
        "ANSINA_SECURITY__CORS__ALLOWED_ORIGINS", '["https://dash.example"]'
    )
    settings = load_settings()
    app = FastAPI()

    added = add_cors_middleware(app, settings)

    assert added is True
    assert len(app.user_middleware) == 1
    # `Middleware.cls` is typed as the structural `_MiddlewareFactory` protocol, which
    # declares no `__name__` — `getattr` mirrors Starlette's own `Middleware.__repr__`
    # for the identical reason.
    assert (
        getattr(app.user_middleware[0].cls, "__name__", None) == CORSMiddleware.__name__
    )
