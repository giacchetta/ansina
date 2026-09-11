"""Suite-wide fixtures: a temp XDG config home (so no test ever touches the real
`~/.config/ansina`), a frozen clock, and `httpx.MockTransport` builders — everything
exposed as a fixture (never a plain module-level import) since `tests/` has no
`__init__.py` (see `[tool.pytest.ini_options]`'s `--import-mode=importlib` comment in
`pyproject.toml`), so cross-file `from tests.conftest import ...` isn't reliable;
fixture injection is. `ansina_tui` never imports `ansina`, so nothing here spins up a
real daemon either — every HTTP call in the suite goes through a mock transport.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from ansina_tui.client import ApiClient

JsonHandler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def tmp_xdg_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point `XDG_CONFIG_HOME` at an empty temp dir, and strip any `ANSINA_*` env var
    that might leak in from the dev machine running the suite."""
    xdg = tmp_path / "xdg-config"
    xdg.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.delenv("ANSINA_TOKEN", raising=False)
    monkeypatch.delenv("ANSINA_HOST", raising=False)
    return xdg


@pytest.fixture
def frozen_now() -> datetime:
    """A fixed instant tests anchor sudo-grant expiry checks to."""
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def clock(frozen_now: datetime) -> Callable[[], datetime]:
    """An injectable `now()` — the same pattern `ansina.auth.sudo` uses so expiry
    logic is tested without real sleeping."""

    def _now() -> datetime:
        return frozen_now

    return _now


@pytest.fixture
def iso(frozen_now: datetime) -> Callable[[timedelta], str]:
    """`frozen_now + offset`, as the ISO 8601 string `hosts.toml` stores."""

    def _iso(offset: timedelta = timedelta()) -> str:
        return (frozen_now + offset).isoformat()

    return _iso


@pytest.fixture
def json_response() -> Callable[..., httpx.Response]:
    def _make(
        status_code: int, body: Any, *, headers: Mapping[str, str] | None = None
    ) -> httpx.Response:
        return httpx.Response(status_code, json=body, headers=headers)

    return _make


RouteKey = str | tuple[str, str]  # "/path" (any method) or ("METHOD", "/path")


@pytest.fixture
def mock_transport() -> Callable[[Mapping[RouteKey, Any]], httpx.MockTransport]:
    """Build an `httpx.MockTransport` dispatching on request path, or on
    `(method, path)` when a route needs to answer differently per verb (issue #32:
    `/auth/me/tokens` is `GET`+`POST`, `/auth/sudo` is `POST`+`DELETE`) — a
    `(method, path)` entry is checked first, then the method-agnostic `path` entry."""

    def _build(
        routes: Mapping[RouteKey, JsonHandler | httpx.Response],
    ) -> httpx.MockTransport:
        def _handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            route = routes.get((request.method, path), routes.get(path))
            if route is None:
                return httpx.Response(
                    404, json={"detail": f"no route for {request.method} {path}"}
                )
            if isinstance(route, httpx.Response):
                return route
            return route(request)

        return httpx.MockTransport(_handler)

    return _build


@pytest.fixture
def captured_requests() -> list[httpx.Request]:
    """Populated by `capturing_transport` — lets a pinning test assert on outgoing
    headers (e.g. that an expired sudo grant is never attached to a request, or that
    no secret appears in one)."""
    return []


@pytest.fixture
def capturing_transport(
    captured_requests: list[httpx.Request],
) -> Callable[[httpx.BaseTransport], httpx.MockTransport]:
    """Wrap any transport (typically one built by `mock_transport`) so every request
    that passes through it is also appended to `captured_requests`."""

    def _wrap(inner: httpx.BaseTransport) -> httpx.MockTransport:
        def _handler(request: httpx.Request) -> httpx.Response:
            captured_requests.append(request)
            return inner.handle_request(request)

        return httpx.MockTransport(_handler)

    return _wrap


@pytest.fixture
def unreachable_transport() -> httpx.MockTransport:
    """A transport that always fails at the connection level, for exercising
    `HostUnreachableError`."""

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return httpx.MockTransport(_handler)


@pytest.fixture
def patch_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[httpx.BaseTransport], None]:
    """Make `ansina_tui.session.build_client` construct its `ApiClient` against a
    given transport instead of a real network socket — the CLI layer has no
    `--transport` flag of its own; this is the test-only seam every command shares
    via `session.build_client` (issue #32; replaces #31's `patch_status_transport`,
    now that `commands/status.py` is built on `session.py` too)."""

    def _patch(fake_transport: httpx.BaseTransport) -> None:
        import ansina_tui.session as session_module

        def _factory(
            host: str,
            *,
            token: str | None = None,
            sudo_token: str | None = None,
            sudo_expires_at: str | None = None,
            now: Callable[[], datetime] = lambda: datetime.now(UTC),
            transport: httpx.BaseTransport | None = None,
        ) -> ApiClient:
            del transport  # the fixture's own transport always wins in tests
            return ApiClient(
                host,
                token=token,
                sudo_token=sudo_token,
                sudo_expires_at=sudo_expires_at,
                now=now,
                transport=fake_transport,
            )

        monkeypatch.setattr(session_module, "ApiClient", _factory)

    return _patch
