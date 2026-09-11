"""Pins `daemon_state.fetch_overview`'s contract: every designed Overview state
(connected, unreachable, insecure `hosts.toml`, no credential, a rejected/forbidden
call, a not-ready daemon) renders as data, never a raised exception — see the
module's own docstring for why that matters to a Textual `@work` worker.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from ansina_tui.client import ApiResponse
from ansina_tui.config import DEFAULT_HOST
from ansina_tui.context import AppContext
from ansina_tui.daemon_state import fetch_overview, readiness_checks, version_display
from ansina_tui.problems import parse_problem


def _ctx(host: str | None = None) -> AppContext:
    return AppContext(host=host, json_output=False, verbose=False)


def _response(status_code: int, body: object | None) -> ApiResponse:
    return ApiResponse(
        status_code=status_code,
        request_id="req-xyz",
        json_body=body,
        problem=parse_problem(body),
        text="" if body is None else json.dumps(body),
        headers={},
    )


# --- readiness_checks / version_display: pure, no transport involved -------------


def test_readiness_checks_extracts_the_checks_dict() -> None:
    ready = _response(200, {"status": "ready", "checks": {"database": True}})
    assert readiness_checks(ready) == {"database": True}


def test_readiness_checks_from_a_503_problem_body_extension_member() -> None:
    ready = _response(
        503,
        {
            "code": "ansina.not_ready",
            "checks": {"database": False, "heart": True},
        },
    )
    assert readiness_checks(ready) == {"database": False, "heart": True}


def test_readiness_checks_returns_empty_when_checks_key_is_missing() -> None:
    ready = _response(200, {"status": "ready"})
    assert readiness_checks(ready) == {}


def test_readiness_checks_returns_empty_when_checks_is_not_a_dict() -> None:
    ready = _response(200, {"status": "ready", "checks": "not-a-dict"})
    assert readiness_checks(ready) == {}


def test_readiness_checks_returns_empty_for_a_non_dict_body() -> None:
    ready = _response(200, None)
    assert readiness_checks(ready) == {}


def test_version_display_formats_name_and_version() -> None:
    version = _response(200, {"name": "ansina", "version": "0.1.0"})
    assert version_display(version) == "ansina 0.1.0"


def test_version_display_falls_back_to_unknown_on_a_failed_response() -> None:
    version = _response(401, {"code": "ansina.unauthorized"})
    assert version_display(version) == "unknown"


def test_version_display_falls_back_to_unknown_for_a_non_dict_body() -> None:
    version = _response(200, None)
    assert version_display(version) == "unknown"


# --- fetch_overview: the full read, driven through a mocked daemon ---------------


def _happy_routes(json_response: Callable[..., httpx.Response]) -> dict[str, Any]:
    return {
        "/healthz": json_response(200, {"status": "ok"}),
        "/readyz": json_response(
            200, {"status": "ready", "checks": {"database": True}}
        ),
        "/version": json_response(200, {"name": "ansina", "version": "0.1.0"}),
        "/auth/me": json_response(
            200,
            {
                "username": "alice",
                "roles": ["read", "maintain"],
                "auth_method": "api_token",
                "sudo_active": True,
            },
        ),
    }


def test_fetch_overview_connected_happy_path(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str, Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    patch_transport(mock_transport(_happy_routes(json_response)))

    snapshot = fetch_overview(_ctx("http://x"), env={"ANSINA_TOKEN": "tok"})

    assert snapshot.host == "http://x"
    assert snapshot.connection == "connected"
    assert snapshot.error is None
    assert snapshot.health.rows == (("status", "ok"),)
    assert snapshot.readiness.rows == (("database", "ok"),)
    assert snapshot.readiness.message is None
    assert snapshot.version.rows == (("version", "ansina 0.1.0"),)
    assert ("username", "alice") in snapshot.identity.rows
    assert ("roles", "read, maintain") in snapshot.identity.rows
    assert ("sudo", "active") in snapshot.identity.rows


def test_fetch_overview_no_credential_stored_skips_auth_me(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str, Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
    capturing_transport: Callable[[httpx.BaseTransport], httpx.MockTransport],
    captured_requests: list[httpx.Request],
) -> None:
    routes = {
        "/healthz": json_response(200, {"status": "ok"}),
        "/readyz": json_response(200, {"status": "ready", "checks": {}}),
        "/version": json_response(401, {"code": "ansina.unauthorized"}),
    }
    patch_transport(capturing_transport(mock_transport(routes)))

    snapshot = fetch_overview(_ctx("http://x"), env={})

    assert snapshot.connection == "connected"
    assert snapshot.identity.rows == ()
    assert snapshot.identity.message is not None
    assert "auth login" in snapshot.identity.message
    assert "ANSINA_TOKEN" in snapshot.identity.message
    assert not any(req.url.path == "/auth/me" for req in captured_requests)


def test_fetch_overview_auth_me_401_renders_the_recognized_message(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str, Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    routes = _happy_routes(json_response)
    routes["/auth/me"] = json_response(401, {"code": "ansina.unauthorized"})
    patch_transport(mock_transport(routes))

    snapshot = fetch_overview(_ctx("http://x"), env={"ANSINA_TOKEN": "bad"})

    assert snapshot.identity.rows == ()
    assert snapshot.identity.message == (
        "Not authenticated. Run `auth login` or set ANSINA_TOKEN."
    )


def test_fetch_overview_version_403_renders_a_permission_message_not_a_dump(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str, Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    routes = _happy_routes(json_response)
    routes["/version"] = json_response(403, {"code": "ansina.forbidden"})
    patch_transport(mock_transport(routes))

    snapshot = fetch_overview(_ctx("http://x"), env={"ANSINA_TOKEN": "tok"})

    assert snapshot.version.rows == ()
    assert snapshot.version.message == "Your role doesn't grant this action."


def test_fetch_overview_readyz_503_lists_failing_checks_with_a_message(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str, Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    routes = _happy_routes(json_response)
    routes["/readyz"] = json_response(
        503,
        {
            "code": "ansina.not_ready",
            "checks": {"database": False, "heart": True},
        },
    )
    patch_transport(mock_transport(routes))

    snapshot = fetch_overview(_ctx("http://x"), env={"ANSINA_TOKEN": "tok"})

    assert ("database", "fail") in snapshot.readiness.rows
    assert ("heart", "ok") in snapshot.readiness.rows
    assert snapshot.readiness.message == "The daemon is not ready yet."


def test_fetch_overview_healthz_failure_renders_a_designed_message(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str, Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    routes = _happy_routes(json_response)
    routes["/healthz"] = json_response(500, {"detail": "boom"})
    patch_transport(mock_transport(routes))

    snapshot = fetch_overview(_ctx("http://x"), env={"ANSINA_TOKEN": "tok"})

    assert snapshot.health.rows == ()
    assert snapshot.health.message == "boom"


def test_fetch_overview_healthz_failure_with_no_problem_body_uses_the_status_code(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str, Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    routes = _happy_routes(json_response)
    routes["/healthz"] = json_response(500, None)
    patch_transport(mock_transport(routes))

    snapshot = fetch_overview(_ctx("http://x"), env={"ANSINA_TOKEN": "tok"})

    assert snapshot.health.message == "Request failed (HTTP 500)."


def test_fetch_overview_version_failure_with_no_problem_body_shows_unknown(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str, Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    routes = _happy_routes(json_response)
    routes["/version"] = json_response(500, None)
    patch_transport(mock_transport(routes))

    snapshot = fetch_overview(_ctx("http://x"), env={"ANSINA_TOKEN": "tok"})

    assert snapshot.version.rows == (("version", "unknown"),)
    assert snapshot.version.message is None


def test_fetch_overview_identity_tolerates_a_non_list_roles_field(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str, Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    routes = _happy_routes(json_response)
    routes["/auth/me"] = json_response(
        200, {"username": "bob", "roles": "read", "auth_method": "api_token"}
    )
    patch_transport(mock_transport(routes))

    snapshot = fetch_overview(_ctx("http://x"), env={"ANSINA_TOKEN": "tok"})

    assert ("roles", "read") in snapshot.identity.rows
    assert ("sudo", "inactive") in snapshot.identity.rows


def test_fetch_overview_unreachable_host_never_raises(
    tmp_xdg_home: Path,
    unreachable_transport: httpx.MockTransport,
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    patch_transport(unreachable_transport)

    snapshot = fetch_overview(_ctx("http://x"), env={})

    assert snapshot.connection == "unreachable"
    assert snapshot.error is not None
    assert "Could not reach" in snapshot.error
    assert snapshot.health.rows == () and snapshot.health.message is None
    assert snapshot.readiness.rows == () and snapshot.readiness.message is None
    assert snapshot.version.rows == () and snapshot.version.message is None
    assert snapshot.identity.rows == () and snapshot.identity.message is None


def test_fetch_overview_insecure_hosts_file_never_raises(tmp_xdg_home: Path) -> None:
    ansina_dir = tmp_xdg_home / "ansina"
    ansina_dir.mkdir()
    hosts_path = ansina_dir / "hosts.toml"
    hosts_path.write_text('[hosts."http://x"]\ntoken = "t"\n')
    os.chmod(hosts_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IROTH)

    snapshot = fetch_overview(_ctx(None), env={})

    assert snapshot.host == DEFAULT_HOST
    assert snapshot.connection == "config error"
    assert snapshot.error is not None
    assert "chmod 600" in snapshot.error
    assert snapshot.identity == snapshot.health  # both the shared empty panel


def test_fetch_overview_defaults_to_os_environ_when_env_is_omitted(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str, Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANSINA_TOKEN", "from-environ")
    patch_transport(mock_transport(_happy_routes(json_response)))

    snapshot = fetch_overview(_ctx("http://x"))

    assert snapshot.connection == "connected"
    assert ("username", "alice") in snapshot.identity.rows
