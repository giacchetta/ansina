"""Pins issue #33's acceptance criteria: `ansina-tui api` reaches any route with no
per-route special-casing, auth is automatic and never overridable, body construction
(`-f`/`--input`) and output (`-i`, `--json`/piped) follow the documented rules, and a
sensitive route with no live sudo grant fails fast — never prompting mid-request."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import typer
from typer.testing import CliRunner

import ansina_tui.commands.api as api_module
from ansina_tui.config import HostEntry, save_hosts
from ansina_tui.exits import ExitCode
from ansina_tui.main import app

runner = CliRunner()

MockTransportFactory = Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport]


def _make_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch `_stdout_is_tty` rather than `sys.stdout` itself — `CliRunner` swaps
    `sys.stdout` per-invocation, so patching the stream from outside `invoke()`
    wouldn't take (the same trap `main._is_interactive`'s docstring documents)."""
    monkeypatch.setattr(api_module, "_stdout_is_tty", lambda: True)


def _never_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails the test immediately (via `catch_exceptions=False`) if `api` ever tries
    to prompt — pinning that it can never block mid-request in its CI/CD role."""

    def _boom(*args: object, **kwargs: object) -> str:
        raise AssertionError("api must never prompt for input")

    monkeypatch.setattr(typer, "prompt", _boom)
    monkeypatch.setattr(typer, "confirm", _boom)


# --- authenticated GET, no hand-written headers (criterion 1) ----------------------


def test_get_sends_authenticated_request_with_only_the_bearer_header(
    tmp_xdg_home: Path,
    mock_transport: MockTransportFactory,
    capturing_transport: Callable[[httpx.BaseTransport], httpx.MockTransport],
    captured_requests: list[httpx.Request],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="my-token")})
    inner = mock_transport({"/version": json_response(200, {"name": "ansina"})})
    patch_transport(capturing_transport(inner))

    result = runner.invoke(app, ["--host", "http://x", "api", "/version"])

    assert result.exit_code == ExitCode.OK
    request = captured_requests[0]
    assert request.method == "GET"
    assert request.headers["authorization"] == "Bearer my-token"
    assert "x-sudo-token" not in request.headers


def test_path_without_leading_slash_is_normalized(
    tmp_xdg_home: Path,
    mock_transport: MockTransportFactory,
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    patch_transport(mock_transport({"/version": json_response(200, {"ok": True})}))

    result = runner.invoke(app, ["--host", "http://x", "api", "version"])

    assert result.exit_code == ExitCode.OK


def test_no_token_stored_still_sends_the_request_with_no_auth_header(
    tmp_xdg_home: Path,
    mock_transport: MockTransportFactory,
    capturing_transport: Callable[[httpx.BaseTransport], httpx.MockTransport],
    captured_requests: list[httpx.Request],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    inner = mock_transport({"/healthz": json_response(200, {"status": "ok"})})
    patch_transport(capturing_transport(inner))

    result = runner.invoke(app, ["--host", "http://x", "api", "/healthz"])

    assert result.exit_code == ExitCode.OK
    assert "authorization" not in captured_requests[0].headers


# --- heart-disabled rendering (criterion 2) -----------------------------------------


def test_post_with_heart_disabled_renders_message_and_exits_1(
    tmp_xdg_home: Path,
    mock_transport: MockTransportFactory,
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    patch_transport(
        mock_transport(
            {
                ("POST", "/heart/tick/pause"): json_response(
                    503,
                    {
                        "code": "ansina.heart.disabled",
                        "title": "Heart Disabled",
                        "status": 503,
                    },
                )
            }
        )
    )

    result = runner.invoke(
        app, ["--host", "http://x", "api", "-X", "POST", "/heart/tick/pause"]
    )

    assert result.exit_code == ExitCode.REQUEST_FAILED
    assert "The Heart is disabled on this daemon." in result.stderr


# --- -f field body + method default (criterion 3) -----------------------------------


def test_fields_build_a_json_object_body_and_default_the_method_to_post(
    tmp_xdg_home: Path,
    mock_transport: MockTransportFactory,
    capturing_transport: Callable[[httpx.BaseTransport], httpx.MockTransport],
    captured_requests: list[httpx.Request],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    inner = mock_transport({("POST", "/auth/users"): json_response(201, {"id": "u1"})})
    patch_transport(capturing_transport(inner))

    result = runner.invoke(
        app,
        [
            "--host",
            "http://x",
            "api",
            "-f",
            "username=bob",
            "-f",
            "active=true",
            "/auth/users",
        ],
    )

    assert result.exit_code == ExitCode.OK
    request = captured_requests[0]
    assert request.method == "POST"
    assert json.loads(request.content) == {"username": "bob", "active": "true"}


# --- --input, raw body from file/stdin (criterion 4) --------------------------------


def test_input_dash_reads_the_raw_body_from_stdin(
    tmp_xdg_home: Path,
    mock_transport: MockTransportFactory,
    capturing_transport: Callable[[httpx.BaseTransport], httpx.MockTransport],
    captured_requests: list[httpx.Request],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    inner = mock_transport(
        {("PATCH", "/auth/users/u1"): json_response(200, {"id": "u1"})}
    )
    patch_transport(capturing_transport(inner))

    result = runner.invoke(
        app,
        ["--host", "http://x", "api", "-X", "PATCH", "/auth/users/u1", "--input", "-"],
        input='{"active": false}',
    )

    assert result.exit_code == ExitCode.OK
    assert captured_requests[0].content == b'{"active": false}'


def test_input_file_reads_the_raw_body_from_disk_and_defaults_to_post(
    tmp_xdg_home: Path,
    tmp_path: Path,
    mock_transport: MockTransportFactory,
    capturing_transport: Callable[[httpx.BaseTransport], httpx.MockTransport],
    captured_requests: list[httpx.Request],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    body_file = tmp_path / "body.json"
    body_file.write_text('{"slug": "ops"}')
    inner = mock_transport({("POST", "/auth/groups"): json_response(201, {"id": "g1"})})
    patch_transport(capturing_transport(inner))

    result = runner.invoke(
        app,
        ["--host", "http://x", "api", "/auth/groups", "--input", str(body_file)],
    )

    assert result.exit_code == ExitCode.OK
    request = captured_requests[0]
    assert request.method == "POST"
    assert request.content == b'{"slug": "ops"}'


def test_input_defaults_content_type_to_json_unless_a_header_already_set_it(
    tmp_xdg_home: Path,
    mock_transport: MockTransportFactory,
    capturing_transport: Callable[[httpx.BaseTransport], httpx.MockTransport],
    captured_requests: list[httpx.Request],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    inner = mock_transport({("POST", "/auth/groups"): json_response(201, {})})
    patch_transport(capturing_transport(inner))

    runner.invoke(
        app,
        ["--host", "http://x", "api", "/auth/groups", "--input", "-"],
        input="{}",
    )

    assert captured_requests[0].headers["content-type"] == "application/json"


def test_input_content_type_header_overrides_the_default(
    tmp_xdg_home: Path,
    mock_transport: MockTransportFactory,
    capturing_transport: Callable[[httpx.BaseTransport], httpx.MockTransport],
    captured_requests: list[httpx.Request],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    inner = mock_transport({("POST", "/auth/groups"): json_response(201, {})})
    patch_transport(capturing_transport(inner))

    runner.invoke(
        app,
        [
            "--host",
            "http://x",
            "api",
            "/auth/groups",
            "--input",
            "-",
            "-H",
            "Content-Type: text/plain",
        ],
        input="plain body",
    )

    assert captured_requests[0].headers["content-type"] == "text/plain"


def test_unreadable_input_file_exits_2(tmp_xdg_home: Path) -> None:
    result = runner.invoke(
        app,
        ["--host", "http://x", "api", "/auth/groups", "--input", "/no/such/file.json"],
    )

    assert result.exit_code == ExitCode.USAGE


# --- -f and --input are mutually exclusive (criterion 5) ----------------------------


def test_field_and_input_together_is_a_usage_error(tmp_xdg_home: Path) -> None:
    result = runner.invoke(
        app,
        [
            "--host",
            "http://x",
            "api",
            "/auth/users",
            "-f",
            "a=1",
            "--input",
            "-",
        ],
        input="{}",
    )

    assert result.exit_code == ExitCode.USAGE
    assert "mutually exclusive" in result.stderr


# --- usage errors (criterion 6) ------------------------------------------------------


def test_malformed_field_exits_2(tmp_xdg_home: Path) -> None:
    result = runner.invoke(
        app, ["--host", "http://x", "api", "/auth/users", "-f", "no-equals-sign"]
    )

    assert result.exit_code == ExitCode.USAGE
    assert "key=value" in result.stderr


def test_malformed_header_exits_2(tmp_xdg_home: Path) -> None:
    result = runner.invoke(
        app, ["--host", "http://x", "api", "/version", "-H", "NoColonHere"]
    )

    assert result.exit_code == ExitCode.USAGE
    assert "Name: value" in result.stderr


def test_full_url_path_is_refused(tmp_xdg_home: Path) -> None:
    result = runner.invoke(
        app, ["--host", "http://x", "api", "http://evil.example/steal"]
    )

    assert result.exit_code == ExitCode.USAGE
    assert "full URL" in result.stderr


# --- sensitive route with no live grant never prompts (criterion 7) -----------------


def test_sensitive_route_without_sudo_exits_4_with_hint_and_never_prompts(
    tmp_xdg_home: Path,
    mock_transport: MockTransportFactory,
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _never_prompt(monkeypatch)
    save_hosts({"http://x": HostEntry(token="maintain-tok")})
    patch_transport(
        mock_transport(
            {
                ("POST", "/auth/users"): json_response(
                    403,
                    {
                        "code": "ansina.auth.sudo_required",
                        "title": "Sudo Required",
                        "status": 403,
                    },
                )
            }
        )
    )

    result = runner.invoke(
        app,
        ["--host", "http://x", "api", "-f", "username=bob", "/auth/users"],
        catch_exceptions=False,
    )

    assert result.exit_code == ExitCode.FORBIDDEN
    assert "ansina-tui auth sudo" in result.stderr


# --- live sudo grant attaches automatically (criterion 8) ---------------------------


def test_live_sudo_grant_is_attached_automatically(
    tmp_xdg_home: Path,
    mock_transport: MockTransportFactory,
    capturing_transport: Callable[[httpx.BaseTransport], httpx.MockTransport],
    captured_requests: list[httpx.Request],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    far_future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    save_hosts(
        {
            "http://x": HostEntry(
                token="maintain-tok",
                sudo_token="sudo-abc",
                sudo_expires_at=far_future,
            )
        }
    )
    inner = mock_transport({("POST", "/auth/users"): json_response(201, {"id": "u1"})})
    patch_transport(capturing_transport(inner))

    result = runner.invoke(
        app, ["--host", "http://x", "api", "-f", "username=bob", "/auth/users"]
    )

    assert result.exit_code == ExitCode.OK
    assert captured_requests[0].headers["x-sudo-token"] == "sudo-abc"


# --- -i vs --json / piped stdout (criterion 9) ---------------------------------------


def test_include_shows_status_and_headers_on_a_tty(
    tmp_xdg_home: Path,
    mock_transport: MockTransportFactory,
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_tty(monkeypatch)
    patch_transport(
        mock_transport(
            {
                "/version": json_response(
                    200, {"name": "ansina"}, headers={"X-Request-Id": "req-9"}
                )
            }
        )
    )

    result = runner.invoke(app, ["--host", "http://x", "api", "-i", "/version"])

    assert result.exit_code == ExitCode.OK
    assert "HTTP 200" in result.stdout
    assert "x-request-id: req-9" in result.stdout
    assert "ansina" in result.stdout


def test_include_is_suppressed_under_json_so_stdout_is_exactly_the_body(
    tmp_xdg_home: Path,
    mock_transport: MockTransportFactory,
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_tty(monkeypatch)  # even on a TTY, --json still wins
    patch_transport(mock_transport({"/version": json_response(200, {"name": "x"})}))

    result = runner.invoke(
        app, ["--host", "http://x", "--json", "api", "-i", "/version"]
    )

    assert result.exit_code == ExitCode.OK
    assert json.loads(result.stdout) == {"name": "x"}


# --- --json on non-2xx pipes the problem document (criterion 10) --------------------


def test_json_mode_on_non_2xx_emits_only_the_problem_document_on_stdout(
    tmp_xdg_home: Path,
    mock_transport: MockTransportFactory,
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    patch_transport(
        mock_transport({"/nope": json_response(404, {"code": "ansina.auth.not_found"})})
    )

    result = runner.invoke(app, ["--host", "http://x", "--json", "api", "/nope"])

    assert result.exit_code == ExitCode.REQUEST_FAILED
    assert json.loads(result.stdout) == {"code": "ansina.auth.not_found"}
    assert "No such token." in result.stderr


# --- a caller-supplied -H cannot silently replace Authorization (criterion 11) ------


def test_caller_supplied_authorization_header_is_overridden_not_silently(
    tmp_xdg_home: Path,
    mock_transport: MockTransportFactory,
    capturing_transport: Callable[[httpx.BaseTransport], httpx.MockTransport],
    captured_requests: list[httpx.Request],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="real-token")})
    inner = mock_transport({"/version": json_response(200, {})})
    patch_transport(capturing_transport(inner))

    result = runner.invoke(
        app,
        [
            "--host",
            "http://x",
            "api",
            "/version",
            "-H",
            "Authorization: Bearer evil",
        ],
    )

    assert result.exit_code == ExitCode.OK
    assert captured_requests[0].headers["authorization"] == "Bearer real-token"
    assert "ignored" in result.stderr
    assert "Authorization" in result.stderr


# --- every route family is exercised (criterion 12) ----------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/healthz",
        "/version",
        "/openapi.json",
        "/heart/tick",
        "/auth/users",
        "/auth/me",
    ],
)
def test_every_route_family_is_reachable(
    path: str,
    tmp_xdg_home: Path,
    mock_transport: MockTransportFactory,
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    patch_transport(mock_transport({path: json_response(200, {"ok": True})}))

    result = runner.invoke(app, ["--host", "http://x", "api", path])

    assert result.exit_code == ExitCode.OK
    assert json.loads(result.stdout) == {"ok": True}


# --- shared failure paths (criterion 13) ---------------------------------------------


def test_unreachable_host_exits_5(
    tmp_xdg_home: Path,
    unreachable_transport: httpx.MockTransport,
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    patch_transport(unreachable_transport)

    result = runner.invoke(app, ["--host", "http://x", "api", "/version"])

    assert result.exit_code == ExitCode.HOST_UNREACHABLE
    assert "Could not reach" in result.stderr


def test_insecure_hosts_file_exits_2(tmp_xdg_home: Path) -> None:
    ansina_dir = tmp_xdg_home / "ansina"
    ansina_dir.mkdir()
    hosts_path = ansina_dir / "hosts.toml"
    hosts_path.write_text('[hosts."http://x"]\ntoken = "t"\n')
    os.chmod(hosts_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IROTH)

    result = runner.invoke(app, ["--host", "http://x", "api", "/version"])

    assert result.exit_code == ExitCode.USAGE
    assert "chmod 600" in result.stderr


# --- rendering branches (criterion 14) ------------------------------------------------


def test_empty_response_body_writes_nothing_to_stdout(
    tmp_xdg_home: Path,
    mock_transport: MockTransportFactory,
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    patch_transport(mock_transport({("DELETE", "/auth/sudo"): httpx.Response(204)}))

    result = runner.invoke(
        app, ["--host", "http://x", "api", "-X", "DELETE", "/auth/sudo"]
    )

    assert result.exit_code == ExitCode.OK
    assert result.stdout == ""


def test_non_json_body_is_passed_through_verbatim(
    tmp_xdg_home: Path,
    mock_transport: MockTransportFactory,
    patch_transport: Callable[[httpx.BaseTransport], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_tty(monkeypatch)  # even pretty-print mode falls back to raw for non-JSON
    patch_transport(
        mock_transport(
            {
                "/version": httpx.Response(
                    200, content=b"plain text", headers={"content-type": "text/plain"}
                )
            }
        )
    )

    result = runner.invoke(app, ["--host", "http://x", "api", "/version"])

    assert result.exit_code == ExitCode.OK
    assert "plain text" in result.stdout


def test_body_is_pretty_printed_on_a_tty_with_no_json_flag(
    tmp_xdg_home: Path,
    mock_transport: MockTransportFactory,
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_tty(monkeypatch)
    patch_transport(mock_transport({"/version": json_response(200, {"a": 1})}))

    result = runner.invoke(app, ["--host", "http://x", "api", "/version"])

    assert result.exit_code == ExitCode.OK
    assert '{\n  "a": 1\n}' in result.stdout


def test_body_is_raw_when_piped_not_on_a_tty(
    tmp_xdg_home: Path,
    mock_transport: MockTransportFactory,
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    # `_stdout_is_tty` is left unpatched — `CliRunner`'s stdout is never a real tty,
    # exercising the "raw when piped" branch without --json.
    patch_transport(mock_transport({"/version": json_response(200, {"a": 1})}))

    result = runner.invoke(app, ["--host", "http://x", "api", "/version"])

    assert result.exit_code == ExitCode.OK
    assert result.stdout.strip() == '{"a":1}'
