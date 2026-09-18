"""`ansina-tui auth totp enroll|status|disable`, over `/auth/me/totp`. See issue #44."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from typer.testing import CliRunner

from ansina_tui.config import HostEntry, load_hosts, save_hosts
from ansina_tui.exits import ExitCode
from ansina_tui.main import app

runner = CliRunner()


# --- enroll ---------------------------------------------------------------------


def test_enroll_with_no_credential_exits_3(tmp_xdg_home: Path) -> None:
    result = runner.invoke(app, ["--host", "http://x", "auth", "totp", "enroll"])

    assert result.exit_code == ExitCode.NOT_AUTHENTICATED


def test_enroll_displays_the_secret_and_uri_once_and_never_persists_it(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="t")})
    patch_transport(
        mock_transport(
            {
                ("POST", "/auth/me/totp"): json_response(
                    201,
                    {
                        "secret": "JBSWY3DPEHPK3PXP",
                        "otpauth_uri": "otpauth://totp/Ansina:bob?secret=JBSWY3DPEHPK3PXP",
                        "digits": 6,
                        "period_seconds": 30,
                    },
                )
            }
        )
    )

    result = runner.invoke(app, ["--host", "http://x", "auth", "totp", "enroll"])

    assert result.exit_code == ExitCode.OK
    assert "JBSWY3DPEHPK3PXP" in result.stdout
    assert "otpauth://totp/Ansina:bob" in result.stdout
    # Nothing local to store for TOTP — only the daemon holds the encrypted secret.
    entry = load_hosts().get("http://x", HostEntry())
    assert entry.token == "t"
    assert entry.sudo_token is None


def test_enroll_json_mode(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="t")})
    patch_transport(
        mock_transport(
            {
                ("POST", "/auth/me/totp"): json_response(
                    201,
                    {
                        "secret": "JBSWY3DPEHPK3PXP",
                        "otpauth_uri": "otpauth://totp/Ansina:bob?secret=x",
                        "digits": 6,
                        "period_seconds": 30,
                    },
                )
            }
        )
    )

    result = runner.invoke(
        app, ["--host", "http://x", "--json", "auth", "totp", "enroll"]
    )

    payload = json.loads(result.stdout)
    assert payload == {
        "secret": "JBSWY3DPEHPK3PXP",
        "otpauth_uri": "otpauth://totp/Ansina:bob?secret=x",
        "digits": 6,
        "period_seconds": 30,
    }


def test_enroll_already_enrolled_reports_the_known_message(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="t")})
    patch_transport(
        mock_transport(
            {
                ("POST", "/auth/me/totp"): json_response(
                    409,
                    {
                        "type": "urn:ansina:error:ansina.auth.totp_already_enrolled",
                        "title": "Conflict",
                        "status": 409,
                        "detail": "already enrolled",
                        "code": "ansina.auth.totp_already_enrolled",
                    },
                )
            }
        )
    )

    result = runner.invoke(app, ["--host", "http://x", "auth", "totp", "enroll"])

    assert result.exit_code == ExitCode.REQUEST_FAILED
    assert "already enrolled" in result.output
    assert "auth totp disable" in result.output


def test_enroll_missing_encryption_key_reports_the_known_message(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="t")})
    patch_transport(
        mock_transport(
            {
                ("POST", "/auth/me/totp"): json_response(
                    503,
                    {
                        "type": "urn:ansina:error:ansina.auth.encryption_key_missing",
                        "title": "Service Unavailable",
                        "status": 503,
                        "detail": "no key configured",
                        "code": "ansina.auth.encryption_key_missing",
                    },
                )
            }
        )
    )

    result = runner.invoke(app, ["--host", "http://x", "auth", "totp", "enroll"])

    assert result.exit_code == ExitCode.REQUEST_FAILED
    assert "security.encryption" in result.output


def test_enroll_unreachable_host_exits_5(
    tmp_xdg_home: Path,
    unreachable_transport: httpx.MockTransport,
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="t")})
    patch_transport(unreachable_transport)

    result = runner.invoke(app, ["--host", "http://x", "auth", "totp", "enroll"])

    assert result.exit_code == ExitCode.HOST_UNREACHABLE


def test_enroll_insecure_hosts_file_exits_2(tmp_xdg_home: Path) -> None:
    ansina_dir = tmp_xdg_home / "ansina"
    ansina_dir.mkdir()
    hosts_path = ansina_dir / "hosts.toml"
    hosts_path.write_text('[hosts."http://x"]\ntoken = "t"\n')
    os.chmod(hosts_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IROTH)

    result = runner.invoke(app, ["--host", "http://x", "auth", "totp", "enroll"])

    assert result.exit_code == ExitCode.USAGE
    assert "chmod 600" in result.output


# --- status -----------------------------------------------------------------------


def test_status_with_no_credential_exits_3(tmp_xdg_home: Path) -> None:
    result = runner.invoke(app, ["--host", "http://x", "auth", "totp", "status"])

    assert result.exit_code == ExitCode.NOT_AUTHENTICATED


def test_status_enrolled(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="t")})
    patch_transport(
        mock_transport(
            {
                ("GET", "/auth/me/totp"): json_response(
                    200,
                    {"enrolled": True, "enrolled_since": "2026-01-01T00:00:00Z"},
                )
            }
        )
    )

    result = runner.invoke(app, ["--host", "http://x", "auth", "totp", "status"])

    assert result.exit_code == ExitCode.OK
    assert "yes" in result.stdout
    assert "2026-01-01T00:00:00Z" in result.stdout


def test_status_not_enrolled(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="t")})
    patch_transport(
        mock_transport(
            {
                ("GET", "/auth/me/totp"): json_response(
                    200, {"enrolled": False, "enrolled_since": None}
                )
            }
        )
    )

    result = runner.invoke(app, ["--host", "http://x", "auth", "totp", "status"])

    assert result.exit_code == ExitCode.OK
    assert "no" in result.stdout


def test_status_json_mode(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="t")})
    patch_transport(
        mock_transport(
            {
                ("GET", "/auth/me/totp"): json_response(
                    200, {"enrolled": False, "enrolled_since": None}
                )
            }
        )
    )

    result = runner.invoke(
        app, ["--host", "http://x", "--json", "auth", "totp", "status"]
    )

    assert json.loads(result.stdout) == {"enrolled": False, "enrolled_since": None}


def test_status_failure_is_reported_via_the_generic_table(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="t")})
    patch_transport(
        mock_transport(
            {
                ("GET", "/auth/me/totp"): json_response(
                    401,
                    {
                        "type": "urn:ansina:error:ansina.unauthorized",
                        "title": "Unauthorized",
                        "status": 401,
                        "detail": "no resolved identity",
                        "code": "ansina.unauthorized",
                    },
                )
            }
        )
    )

    result = runner.invoke(app, ["--host", "http://x", "auth", "totp", "status"])

    assert result.exit_code == ExitCode.NOT_AUTHENTICATED


def test_status_unreachable_host_exits_5(
    tmp_xdg_home: Path,
    unreachable_transport: httpx.MockTransport,
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="t")})
    patch_transport(unreachable_transport)

    result = runner.invoke(app, ["--host", "http://x", "auth", "totp", "status"])

    assert result.exit_code == ExitCode.HOST_UNREACHABLE


def test_status_insecure_hosts_file_exits_2(tmp_xdg_home: Path) -> None:
    ansina_dir = tmp_xdg_home / "ansina"
    ansina_dir.mkdir()
    hosts_path = ansina_dir / "hosts.toml"
    hosts_path.write_text('[hosts."http://x"]\ntoken = "t"\n')
    os.chmod(hosts_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IROTH)

    result = runner.invoke(app, ["--host", "http://x", "auth", "totp", "status"])

    assert result.exit_code == ExitCode.USAGE


# --- disable ------------------------------------------------------------------------


def test_disable_with_no_credential_exits_3(tmp_xdg_home: Path) -> None:
    result = runner.invoke(app, ["--host", "http://x", "auth", "totp", "disable"])

    assert result.exit_code == ExitCode.NOT_AUTHENTICATED


def test_disable_succeeds_with_a_live_sudo_grant(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="t")})
    patch_transport(
        mock_transport({("DELETE", "/auth/me/totp"): json_response(204, None)})
    )

    result = runner.invoke(app, ["--host", "http://x", "auth", "totp", "disable"])

    assert result.exit_code == ExitCode.OK
    assert "disabled" in result.output


def test_disable_json_mode(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="t")})
    patch_transport(
        mock_transport({("DELETE", "/auth/me/totp"): json_response(204, None)})
    )

    result = runner.invoke(
        app, ["--host", "http://x", "--json", "auth", "totp", "disable"]
    )

    assert json.loads(result.stdout) == {"enrolled": False}


def test_disable_without_a_live_grant_surfaces_sudo_required(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    """AC #3: no local confirmation prompt — the daemon's own 403
    `ansina.auth.sudo_required` (unmodified `failures.report()` path) is the only
    gate, exactly like every other sensitive `auth.*` mutation."""
    save_hosts({"http://x": HostEntry(token="t")})
    patch_transport(
        mock_transport(
            {
                ("DELETE", "/auth/me/totp"): json_response(
                    403,
                    {
                        "type": "urn:ansina:error:ansina.auth.sudo_required",
                        "title": "Forbidden",
                        "status": 403,
                        "detail": "sudo required",
                        "code": "ansina.auth.sudo_required",
                    },
                )
            }
        )
    )

    result = runner.invoke(app, ["--host", "http://x", "auth", "totp", "disable"])

    assert result.exit_code == ExitCode.FORBIDDEN
    assert "auth sudo" in result.output


def test_disable_unreachable_host_exits_5(
    tmp_xdg_home: Path,
    unreachable_transport: httpx.MockTransport,
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="t")})
    patch_transport(unreachable_transport)

    result = runner.invoke(app, ["--host", "http://x", "auth", "totp", "disable"])

    assert result.exit_code == ExitCode.HOST_UNREACHABLE


def test_disable_insecure_hosts_file_exits_2(tmp_xdg_home: Path) -> None:
    ansina_dir = tmp_xdg_home / "ansina"
    ansina_dir.mkdir()
    hosts_path = ansina_dir / "hosts.toml"
    hosts_path.write_text('[hosts."http://x"]\ntoken = "t"\n')
    os.chmod(hosts_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IROTH)

    result = runner.invoke(app, ["--host", "http://x", "auth", "totp", "disable"])

    assert result.exit_code == ExitCode.USAGE
