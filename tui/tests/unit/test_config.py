from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from ansina_tui.config import (
    DEFAULT_HOST,
    Config,
    HostEntry,
    InsecureCredentialsFileError,
    config_dir,
    load_config,
    load_hosts,
    resolve_host,
    resolve_token,
    save_config,
    save_hosts,
)


def test_config_dir_honors_xdg_config_home(tmp_xdg_home: Path) -> None:
    assert config_dir() == tmp_xdg_home / "ansina"


def test_config_dir_defaults_to_dot_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config_dir() == tmp_path / ".config" / "ansina"


def test_load_config_defaults_when_file_absent(tmp_xdg_home: Path) -> None:
    assert load_config() == Config(default_host=None)


def test_load_config_reads_default_host(tmp_xdg_home: Path) -> None:
    ansina_dir = tmp_xdg_home / "ansina"
    ansina_dir.mkdir()
    (ansina_dir / "config.toml").write_text('default_host = "http://example:9000"\n')
    assert load_config().default_host == "http://example:9000"


def test_load_hosts_empty_when_file_absent(tmp_xdg_home: Path) -> None:
    assert load_hosts() == {}


def test_load_hosts_refuses_group_readable_file(tmp_xdg_home: Path) -> None:
    ansina_dir = tmp_xdg_home / "ansina"
    ansina_dir.mkdir()
    hosts_path = ansina_dir / "hosts.toml"
    hosts_path.write_text('[hosts."http://x"]\ntoken = "t"\n')
    os.chmod(hosts_path, 0o644)

    with pytest.raises(InsecureCredentialsFileError) as exc_info:
        load_hosts()

    assert "chmod 600" in str(exc_info.value)
    assert str(hosts_path) in str(exc_info.value)


def test_load_hosts_returns_empty_when_hosts_key_is_not_a_table(
    tmp_xdg_home: Path,
) -> None:
    ansina_dir = tmp_xdg_home / "ansina"
    ansina_dir.mkdir()
    hosts_path = ansina_dir / "hosts.toml"
    hosts_path.write_text('hosts = "not-a-table"\n')
    os.chmod(hosts_path, 0o600)

    assert load_hosts() == {}


def test_load_hosts_skips_a_malformed_entry_gracefully(tmp_xdg_home: Path) -> None:
    ansina_dir = tmp_xdg_home / "ansina"
    ansina_dir.mkdir()
    hosts_path = ansina_dir / "hosts.toml"
    hosts_path.write_text('[hosts]\n"http://x" = "not-a-table"\n')
    os.chmod(hosts_path, 0o600)

    assert load_hosts() == {"http://x": HostEntry()}


def test_save_hosts_writes_at_mode_0600(tmp_xdg_home: Path) -> None:
    save_hosts({"http://127.0.0.1:8000": HostEntry(token="secret")})
    path = tmp_xdg_home / "ansina" / "hosts.toml"
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_save_hosts_fixes_a_stale_insecure_mode(tmp_xdg_home: Path) -> None:
    ansina_dir = tmp_xdg_home / "ansina"
    ansina_dir.mkdir()
    path = ansina_dir / "hosts.toml"
    path.write_text("")
    os.chmod(path, 0o644)

    save_hosts({"http://127.0.0.1:8000": HostEntry(token="secret")})

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_save_then_load_hosts_round_trips_every_field(tmp_xdg_home: Path) -> None:
    entry = HostEntry(
        token="tok-123",
        token_id="cred-1",
        username="alice",
        roles=("admin", "read"),
        sudo_token="sudo-abc",
        sudo_expires_at="2026-01-01T12:05:00+00:00",
    )
    save_hosts({"http://127.0.0.1:8000": entry})

    loaded = load_hosts()

    assert loaded == {"http://127.0.0.1:8000": entry}


def test_host_entry_token_id_defaults_to_none() -> None:
    assert HostEntry(token="t").token_id is None


def test_dump_toml_escapes_quotes_and_backslashes(tmp_xdg_home: Path) -> None:
    entry = HostEntry(token='has "quotes" and \\backslash\\')
    save_hosts({"http://x": entry})

    loaded = load_hosts()

    assert loaded["http://x"].token == 'has "quotes" and \\backslash\\'


def test_save_hosts_omits_absent_fields(tmp_xdg_home: Path) -> None:
    save_hosts({"http://x": HostEntry(token="only-token")})
    loaded = load_hosts()
    assert loaded["http://x"] == HostEntry(token="only-token")


def test_resolve_host_precedence_cli_flag_wins(tmp_xdg_home: Path) -> None:
    config = Config(default_host="http://config-default:1")
    env = {"ANSINA_HOST": "http://env:2"}
    assert resolve_host("http://cli:3", env, config) == "http://cli:3"


def test_resolve_host_precedence_env_var_next(tmp_xdg_home: Path) -> None:
    config = Config(default_host="http://config-default:1")
    env = {"ANSINA_HOST": "http://env:2"}
    assert resolve_host(None, env, config) == "http://env:2"


def test_resolve_host_precedence_config_default_next(tmp_xdg_home: Path) -> None:
    config = Config(default_host="http://config-default:1")
    assert resolve_host(None, {}, config) == "http://config-default:1"


def test_resolve_host_falls_back_to_builtin_default(tmp_xdg_home: Path) -> None:
    assert resolve_host(None, {}, Config()) == DEFAULT_HOST


def test_resolve_token_env_beats_stored_token() -> None:
    hosts = {"http://x": HostEntry(token="stored")}
    env = {"ANSINA_TOKEN": "env-token"}
    assert resolve_token("http://x", hosts, env) == "env-token"


def test_resolve_token_falls_back_to_stored_token() -> None:
    hosts = {"http://x": HostEntry(token="stored")}
    assert resolve_token("http://x", hosts, {}) == "stored"


def test_resolve_token_none_when_nothing_stored() -> None:
    assert resolve_token("http://x", {}, {}) is None


def test_save_config_writes_default_host(tmp_xdg_home: Path) -> None:
    save_config(Config(default_host="http://example:9000"))

    assert load_config().default_host == "http://example:9000"


def test_save_config_writes_nothing_when_default_host_is_none(
    tmp_xdg_home: Path,
) -> None:
    save_config(Config(default_host=None))

    path = tmp_xdg_home / "ansina" / "config.toml"
    assert path.read_text() == ""
    assert load_config() == Config(default_host=None)


def test_env_token_is_never_written_back_to_disk(tmp_xdg_home: Path) -> None:
    """`ANSINA_TOKEN` is resolved separately from `HostEntry` (see `resolve_token`) —
    a `save_hosts` call never has it to write in the first place."""
    hosts = {"http://x": HostEntry(token="stored-token")}
    save_hosts(hosts)

    content = (tmp_xdg_home / "ansina" / "hosts.toml").read_text()

    assert "env-token-value" not in content
