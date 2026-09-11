from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from ansina_tui.config import HostEntry, load_config, load_hosts, save_hosts
from ansina_tui.exits import ExitCode
from ansina_tui.main import app

runner = CliRunner()


def test_login_stores_credential_and_reports_identity(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    patch_transport(
        mock_transport(
            {
                "/auth/me": json_response(
                    200,
                    {
                        "user_id": "u1",
                        "username": "alice",
                        "display_name": "Alice",
                        "roles": ["read", "write"],
                        "auth_method": "api_token",
                        "sudo_active": False,
                    },
                )
            }
        )
    )

    result = runner.invoke(
        app, ["--host", "http://x", "auth", "login", "--with-token"], input="tok-1\n"
    )

    assert result.exit_code == ExitCode.OK
    assert "alice" in result.stdout
    hosts = load_hosts()
    assert hosts["http://x"].token == "tok-1"
    assert hosts["http://x"].username == "alice"
    assert hosts["http://x"].roles == ("read", "write")


def test_login_sets_default_host_on_first_success(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    patch_transport(
        mock_transport(
            {
                "/auth/me": json_response(
                    200,
                    {
                        "username": "alice",
                        "roles": ["admin"],
                        "auth_method": "api_token",
                    },
                )
            }
        )
    )

    runner.invoke(
        app, ["--host", "http://x", "auth", "login", "--with-token"], input="tok-1\n"
    )

    assert load_config().default_host == "http://x"


def test_login_rejected_token_stores_nothing_and_exits_3(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    patch_transport(
        mock_transport(
            {
                "/auth/me": json_response(
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

    result = runner.invoke(
        app,
        ["--host", "http://x", "auth", "login", "--with-token"],
        input="bad-token\n",
    )

    assert result.exit_code == ExitCode.NOT_AUTHENTICATED
    assert "rejected" in result.output
    assert load_hosts() == {}


def test_login_json_mode_reports_the_new_identity(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    patch_transport(
        mock_transport(
            {
                "/auth/me": json_response(
                    200,
                    {
                        "username": "alice",
                        "roles": ["read"],
                        "auth_method": "api_token",
                    },
                )
            }
        )
    )

    result = runner.invoke(
        app,
        ["--host", "http://x", "--json", "auth", "login", "--with-token"],
        input="tok-1\n",
    )

    payload = json.loads(result.stdout)
    assert payload["username"] == "alice"
    assert payload["host"] == "http://x"


def test_login_empty_token_exits_2_and_stores_nothing(tmp_xdg_home: Path) -> None:
    result = runner.invoke(
        app, ["--host", "http://x", "auth", "login", "--with-token"], input="\n"
    )

    assert result.exit_code == ExitCode.USAGE
    assert load_hosts() == {}


def test_login_unreachable_host_exits_5(
    tmp_xdg_home: Path,
    unreachable_transport: httpx.MockTransport,
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    patch_transport(unreachable_transport)

    result = runner.invoke(
        app, ["--host", "http://x", "auth", "login", "--with-token"], input="tok\n"
    )

    assert result.exit_code == ExitCode.HOST_UNREACHABLE


def test_login_insecure_hosts_file_exits_2(tmp_xdg_home: Path) -> None:
    ansina_dir = tmp_xdg_home / "ansina"
    ansina_dir.mkdir()
    hosts_path = ansina_dir / "hosts.toml"
    hosts_path.write_text('[hosts."http://x"]\ntoken = "t"\n')
    os.chmod(hosts_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IROTH)

    result = runner.invoke(app, ["auth", "login", "--with-token"], input="tok\n")

    assert result.exit_code == ExitCode.USAGE
    assert "chmod 600" in result.output


def test_token_never_appears_in_login_output_even_when_verbose(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    patch_transport(
        mock_transport(
            {
                "/auth/me": json_response(
                    200,
                    {
                        "username": "alice",
                        "roles": ["read"],
                        "auth_method": "api_token",
                    },
                )
            }
        )
    )
    secret = "super-secret-token-value"

    result = runner.invoke(
        app,
        ["--host", "http://x", "--verbose", "auth", "login", "--with-token"],
        input=f"{secret}\n",
    )

    assert secret not in result.output


def test_logout_removes_stored_entry_and_reports_local_only_caveat(
    tmp_xdg_home: Path,
) -> None:
    save_hosts({"http://x": HostEntry(token="tok", token_id="cred-1")})

    result = runner.invoke(app, ["--host", "http://x", "auth", "logout"])

    assert result.exit_code == ExitCode.OK
    assert "local only" in result.output
    assert "http://x" not in load_hosts()


def test_logout_is_idempotent_for_a_host_with_nothing_stored(
    tmp_xdg_home: Path,
) -> None:
    result = runner.invoke(app, ["--host", "http://x", "auth", "logout"])

    assert result.exit_code == ExitCode.OK


def test_logout_warns_when_ansina_token_env_is_set(
    tmp_xdg_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSINA_TOKEN", "env-token")

    result = runner.invoke(app, ["--host", "http://x", "auth", "logout"])

    assert result.exit_code == ExitCode.OK
    assert "ANSINA_TOKEN" in result.output


def test_logout_json_mode_reports_removal(tmp_xdg_home: Path) -> None:
    save_hosts({"http://x": HostEntry(token="tok")})

    result = runner.invoke(app, ["--host", "http://x", "--json", "auth", "logout"])

    payload = json.loads(result.stdout)
    assert payload == {"host": "http://x", "removed": True}


def test_logout_insecure_hosts_file_exits_2(tmp_xdg_home: Path) -> None:
    ansina_dir = tmp_xdg_home / "ansina"
    ansina_dir.mkdir()
    hosts_path = ansina_dir / "hosts.toml"
    hosts_path.write_text('[hosts."http://x"]\ntoken = "t"\n')
    os.chmod(hosts_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IROTH)

    result = runner.invoke(app, ["--host", "http://x", "auth", "logout"])

    assert result.exit_code == ExitCode.USAGE
    assert "chmod 600" in result.output


def test_login_help_never_touches_the_network(tmp_xdg_home: Path) -> None:
    result = runner.invoke(app, ["auth", "login", "--help"])

    assert result.exit_code == 0
    assert "Usage" in result.stdout
