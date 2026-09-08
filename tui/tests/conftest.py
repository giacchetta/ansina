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


@pytest.fixture
def mock_transport() -> Callable[[Mapping[str, Any]], httpx.MockTransport]:
    """Build an `httpx.MockTransport` dispatching on request path only (every route
    this project talks to is a fixed, method-agnostic path in day-0 scope)."""

    def _build(
        routes: Mapping[str, JsonHandler | httpx.Response],
    ) -> httpx.MockTransport:
        def _handler(request: httpx.Request) -> httpx.Response:
            route = routes.get(request.url.path)
            if route is None:
                return httpx.Response(
                    404, json={"detail": f"no route for {request.url.path}"}
                )
            if isinstance(route, httpx.Response):
                return route
            return route(request)

        return httpx.MockTransport(_handler)

    return _build


@pytest.fixture
def unreachable_transport() -> httpx.MockTransport:
    """A transport that always fails at the connection level, for exercising
    `HostUnreachableError`."""

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return httpx.MockTransport(_handler)


@pytest.fixture
def patch_status_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[httpx.BaseTransport], None]:
    """Make `commands.status.status_command` build its `ApiClient` against a given
    transport instead of a real network socket — the CLI layer has no `--transport`
    flag of its own; this is the test-only seam."""

    def _patch(transport: httpx.BaseTransport) -> None:
        import ansina_tui.commands.status as status_module

        def _factory(host: str, *, token: str | None = None) -> ApiClient:
            return ApiClient(host, token=token, transport=transport)

        monkeypatch.setattr(status_module, "ApiClient", _factory)

    return _patch
