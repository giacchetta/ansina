from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import typer
from typer.testing import CliRunner

import ansina_tui.commands.auth.tokens as tokens_module
from ansina_tui.config import HostEntry, load_hosts, save_hosts
from ansina_tui.context import AppContext
from ansina_tui.exits import ExitCode
from ansina_tui.main import app
from ansina_tui.output import Emitter
from ansina_tui.session import Session, resolve_session

runner = CliRunner()


class _FakeTtyStream:
    def isatty(self) -> bool:
        return True


class _FakeInteractiveSys:
    """Stands in for the `sys` module inside `tokens_module` so its interactive-only
    branches (`_resolve_store_choice`, `_confirm_self_revoke`) can be exercised
    without CliRunner's stdin/stdout, which are never a real TTY."""

    stdin = _FakeTtyStream()
    stdout = _FakeTtyStream()


def _session(tmp_xdg_home: Path) -> Session:
    return resolve_session(
        AppContext(host="http://x", json_output=False, verbose=False), env={}
    )


def test_mint_with_no_credential_exits_3(tmp_xdg_home: Path) -> None:
    result = runner.invoke(app, ["--host", "http://x", "auth", "token", "mint"])

    assert result.exit_code == ExitCode.NOT_AUTHENTICATED


def test_mint_displays_the_token_once_and_stores_it(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="admin-tok")})
    patch_transport(
        mock_transport(
            {
                ("POST", "/auth/me/tokens"): json_response(
                    201,
                    {
                        "id": "cred-2",
                        "label": "laptop",
                        "created_at": "2026-01-01T00:00:00Z",
                        "last_used_at": None,
                        "token": "raw-new-token",
                    },
                )
            }
        )
    )

    result = runner.invoke(
        app,
        ["--host", "http://x", "auth", "token", "mint", "--label", "laptop", "--store"],
    )

    assert result.exit_code == ExitCode.OK
    assert "raw-new-token" in result.stdout
    entry = load_hosts()["http://x"]
    assert entry.token == "raw-new-token"
    assert entry.token_id == "cred-2"


def test_mint_defaults_to_not_storing_when_non_interactive_and_unspecified(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="admin-tok")})
    patch_transport(
        mock_transport(
            {
                ("POST", "/auth/me/tokens"): json_response(
                    201,
                    {
                        "id": "cred-2",
                        "label": "",
                        "created_at": "2026-01-01T00:00:00Z",
                        "last_used_at": None,
                        "token": "raw-new-token",
                    },
                )
            }
        )
    )

    result = runner.invoke(app, ["--host", "http://x", "auth", "token", "mint"])

    assert result.exit_code == ExitCode.OK
    assert load_hosts()["http://x"].token == "admin-tok"


def test_mint_no_store_leaves_the_existing_credential_untouched(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="admin-tok")})
    patch_transport(
        mock_transport(
            {
                ("POST", "/auth/me/tokens"): json_response(
                    201,
                    {
                        "id": "cred-2",
                        "label": "",
                        "created_at": "2026-01-01T00:00:00Z",
                        "last_used_at": None,
                        "token": "raw-new-token",
                    },
                )
            }
        )
    )

    result = runner.invoke(
        app, ["--host", "http://x", "auth", "token", "mint", "--no-store"]
    )

    assert result.exit_code == ExitCode.OK
    assert load_hosts()["http://x"].token == "admin-tok"


def test_mint_json_mode_carries_the_token_in_the_payload(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="admin-tok")})
    patch_transport(
        mock_transport(
            {
                ("POST", "/auth/me/tokens"): json_response(
                    201,
                    {
                        "id": "cred-2",
                        "label": "ci",
                        "created_at": "2026-01-01T00:00:00Z",
                        "last_used_at": None,
                        "token": "raw-new-token",
                    },
                )
            }
        )
    )

    result = runner.invoke(
        app,
        [
            "--host",
            "http://x",
            "--json",
            "auth",
            "token",
            "mint",
            "--label",
            "ci",
            "--no-store",
        ],
    )

    payload = json.loads(result.stdout)
    assert payload["token"] == "raw-new-token"
    assert payload["stored"] is False


def test_mint_bootstrap_identity_403_names_the_real_fix(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="bootstrap-tok")})
    patch_transport(
        mock_transport(
            {
                ("POST", "/auth/me/tokens"): json_response(
                    403,
                    {
                        "type": "urn:ansina:error:ansina.auth.bootstrap_identity",
                        "title": "BootstrapIdentityError",
                        "status": 403,
                        "detail": "bootstrap identity",
                        "code": "ansina.auth.bootstrap_identity",
                    },
                )
            }
        )
    )

    result = runner.invoke(app, ["--host", "http://x", "auth", "token", "mint"])

    assert result.exit_code == ExitCode.FORBIDDEN
    assert "break-glass" in result.output
    assert "configured admin" in result.output


def test_list_with_no_credential_exits_3(tmp_xdg_home: Path) -> None:
    result = runner.invoke(app, ["--host", "http://x", "auth", "token", "list"])

    assert result.exit_code == ExitCode.NOT_AUTHENTICATED


def test_list_renders_metadata_and_marks_the_current_token(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="tok", token_id="cred-1")})
    patch_transport(
        mock_transport(
            {
                ("GET", "/auth/me/tokens"): json_response(
                    200,
                    [
                        {
                            "id": "cred-1",
                            "label": "laptop",
                            "created_at": "2026-01-01T00:00:00Z",
                            "last_used_at": "2026-01-02T00:00:00Z",
                        },
                        {
                            "id": "cred-2",
                            "label": "",
                            "created_at": "2026-01-01T00:00:00Z",
                            "last_used_at": None,
                        },
                    ],
                )
            }
        )
    )

    result = runner.invoke(app, ["--host", "http://x", "auth", "token", "list"])

    assert result.exit_code == ExitCode.OK
    assert "cred-1" in result.stdout
    assert "(current)" in result.stdout
    assert "never" in result.stdout


def test_list_json_mode_returns_the_raw_token_list(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="tok")})
    patch_transport(
        mock_transport(
            {
                ("GET", "/auth/me/tokens"): json_response(
                    200,
                    [
                        {
                            "id": "cred-1",
                            "label": "laptop",
                            "created_at": "2026-01-01T00:00:00Z",
                            "last_used_at": None,
                        }
                    ],
                )
            }
        )
    )

    result = runner.invoke(
        app, ["--host", "http://x", "--json", "auth", "token", "list"]
    )

    payload = json.loads(result.stdout)
    assert payload["tokens"][0]["id"] == "cred-1"


def test_list_never_shows_the_bearer_token_used_to_authenticate(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    secret = "super-secret-bearer-value"
    save_hosts({"http://x": HostEntry(token=secret)})
    patch_transport(
        mock_transport(
            {
                ("GET", "/auth/me/tokens"): json_response(
                    200,
                    [
                        {
                            "id": "cred-1",
                            "label": "laptop",
                            "created_at": "2026-01-01T00:00:00Z",
                            "last_used_at": None,
                        }
                    ],
                )
            }
        )
    )

    result = runner.invoke(
        app, ["--host", "http://x", "--verbose", "auth", "token", "list"]
    )

    assert secret not in result.output


def test_revoke_with_no_credential_exits_3(tmp_xdg_home: Path) -> None:
    result = runner.invoke(
        app, ["--host", "http://x", "auth", "token", "revoke", "cred-1"]
    )

    assert result.exit_code == ExitCode.NOT_AUTHENTICATED


def test_revoke_a_different_token_needs_no_confirmation(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="tok", token_id="cred-1")})
    patch_transport(
        mock_transport({("DELETE", "/auth/me/tokens/cred-2"): json_response(204, None)})
    )

    result = runner.invoke(
        app, ["--host", "http://x", "auth", "token", "revoke", "cred-2"]
    )

    assert result.exit_code == ExitCode.OK
    assert load_hosts()["http://x"].token == "tok"


def test_revoke_own_current_token_with_yes_clears_the_local_entry(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="tok", token_id="cred-1")})
    patch_transport(
        mock_transport({("DELETE", "/auth/me/tokens/cred-1"): json_response(204, None)})
    )

    result = runner.invoke(
        app, ["--host", "http://x", "auth", "token", "revoke", "cred-1", "--yes"]
    )

    assert result.exit_code == ExitCode.OK
    entry = load_hosts()["http://x"]
    assert entry.token is None
    assert entry.token_id is None


def test_revoke_own_current_token_without_yes_non_interactive_exits_2(
    tmp_xdg_home: Path,
) -> None:
    save_hosts({"http://x": HostEntry(token="tok", token_id="cred-1")})

    result = runner.invoke(
        app, ["--host", "http://x", "auth", "token", "revoke", "cred-1"]
    )

    assert result.exit_code == ExitCode.USAGE


def test_revoke_missing_token_exits_1(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="tok", token_id="cred-1")})
    patch_transport(
        mock_transport(
            {
                ("DELETE", "/auth/me/tokens/cred-9"): json_response(
                    404,
                    {
                        "type": "urn:ansina:error:ansina.auth.not_found",
                        "title": "NotFoundError",
                        "status": 404,
                        "detail": "no such token",
                        "code": "ansina.auth.not_found",
                    },
                )
            }
        )
    )

    result = runner.invoke(
        app, ["--host", "http://x", "auth", "token", "revoke", "cred-9"]
    )

    assert result.exit_code == ExitCode.REQUEST_FAILED


def test_revoke_json_mode(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="tok", token_id="cred-1")})
    patch_transport(
        mock_transport({("DELETE", "/auth/me/tokens/cred-2"): json_response(204, None)})
    )

    result = runner.invoke(
        app,
        ["--host", "http://x", "--json", "auth", "token", "revoke", "cred-2"],
    )

    assert json.loads(result.stdout) == {"id": "cred-2", "revoked": True}


def test_mint_unreachable_host_exits_5(
    tmp_xdg_home: Path,
    unreachable_transport: httpx.MockTransport,
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="tok")})
    patch_transport(unreachable_transport)

    result = runner.invoke(app, ["--host", "http://x", "auth", "token", "mint"])

    assert result.exit_code == ExitCode.HOST_UNREACHABLE


def test_list_unreachable_host_exits_5(
    tmp_xdg_home: Path,
    unreachable_transport: httpx.MockTransport,
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="tok")})
    patch_transport(unreachable_transport)

    result = runner.invoke(app, ["--host", "http://x", "auth", "token", "list"])

    assert result.exit_code == ExitCode.HOST_UNREACHABLE


def test_revoke_unreachable_host_exits_5(
    tmp_xdg_home: Path,
    unreachable_transport: httpx.MockTransport,
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="tok", token_id="cred-1")})
    patch_transport(unreachable_transport)

    result = runner.invoke(
        app, ["--host", "http://x", "auth", "token", "revoke", "cred-2"]
    )

    assert result.exit_code == ExitCode.HOST_UNREACHABLE


def _write_insecure_hosts_file(tmp_xdg_home: Path) -> None:
    ansina_dir = tmp_xdg_home / "ansina"
    ansina_dir.mkdir()
    hosts_path = ansina_dir / "hosts.toml"
    hosts_path.write_text('[hosts."http://x"]\ntoken = "t"\n')
    os.chmod(hosts_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IROTH)


def test_mint_insecure_hosts_file_exits_2(tmp_xdg_home: Path) -> None:
    _write_insecure_hosts_file(tmp_xdg_home)

    result = runner.invoke(app, ["--host", "http://x", "auth", "token", "mint"])

    assert result.exit_code == ExitCode.USAGE
    assert "chmod 600" in result.output


def test_list_insecure_hosts_file_exits_2(tmp_xdg_home: Path) -> None:
    _write_insecure_hosts_file(tmp_xdg_home)

    result = runner.invoke(app, ["--host", "http://x", "auth", "token", "list"])

    assert result.exit_code == ExitCode.USAGE
    assert "chmod 600" in result.output


def test_revoke_insecure_hosts_file_exits_2(tmp_xdg_home: Path) -> None:
    _write_insecure_hosts_file(tmp_xdg_home)

    result = runner.invoke(
        app, ["--host", "http://x", "auth", "token", "revoke", "cred-1"]
    )

    assert result.exit_code == ExitCode.USAGE
    assert "chmod 600" in result.output


def test_list_failure_is_reported_via_the_generic_table(
    tmp_xdg_home: Path,
    mock_transport: Callable[[dict[str | tuple[str, str], Any]], httpx.MockTransport],
    json_response: Callable[..., httpx.Response],
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    save_hosts({"http://x": HostEntry(token="stale")})
    patch_transport(
        mock_transport(
            {
                ("GET", "/auth/me/tokens"): json_response(
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

    result = runner.invoke(app, ["--host", "http://x", "auth", "token", "list"])

    assert result.exit_code == ExitCode.NOT_AUTHENTICATED


def test_resolve_store_choice_prompts_when_interactive_and_unspecified(
    tmp_xdg_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tokens_module, "sys", _FakeInteractiveSys())
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: True)

    result = tokens_module._resolve_store_choice(_session(tmp_xdg_home), None)

    assert result is True


def test_confirm_self_revoke_prompts_when_interactive(
    tmp_xdg_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tokens_module, "sys", _FakeInteractiveSys())
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: False)

    result = tokens_module._confirm_self_revoke(
        _session(tmp_xdg_home), "cred-1", Emitter()
    )

    assert result is False


def test_revoke_self_declined_confirmation_aborts_without_revoking(
    tmp_xdg_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_hosts({"http://x": HostEntry(token="t", token_id="cred-1")})
    monkeypatch.setattr(tokens_module, "_confirm_self_revoke", lambda *a, **k: False)

    result = runner.invoke(
        app, ["--host", "http://x", "auth", "token", "revoke", "cred-1"]
    )

    assert result.exit_code == ExitCode.OK
    assert "Aborted" in result.output
    assert load_hosts()["http://x"].token == "t"
