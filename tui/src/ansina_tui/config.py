"""The XDG config store: `config.toml` (non-secret) and `hosts.toml` (credentials).

Python 3.14's stdlib `tomllib` is read-only, and no TOML-writer dependency was added
for this narrow, fully-known schema (see `pyproject.toml`'s dependency comment) — so
`hosts.toml` is serialized by the small hand-rolled `_dump_toml` below rather than a
general-purpose library.

`ANSINA_TOKEN`/`ANSINA_HOST` are resolved separately, at request time, by
`resolve_host`/`resolve_token` — they never enter a `HostEntry`, which is structurally
why they can never be written back to disk by `save_hosts`.
"""

from __future__ import annotations

import os
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOST = "http://127.0.0.1:8000"

_CONFIG_FILE = "config.toml"
_HOSTS_FILE = "hosts.toml"

# Any group/other permission bit set is refused outright — the file holds bearer
# tokens and sudo grants.
_INSECURE_MODE_MASK = 0o077


class InsecureCredentialsFileError(Exception):
    """`hosts.toml` is readable by someone other than its owner."""

    def __init__(self, path: Path, mode: int) -> None:
        self.path = path
        self.mode = mode
        super().__init__(
            f"{path} is readable by group or other (mode {oct(mode)}). "
            f"Refusing to read credentials from it. Fix with: chmod 600 {path}"
        )


@dataclass(frozen=True, slots=True)
class Config:
    """`config.toml` — no secrets here, ever."""

    default_host: str | None = None


@dataclass(frozen=True, slots=True)
class HostEntry:
    """One host's stored credentials in `hosts.toml`."""

    token: str | None = None
    username: str | None = None
    roles: tuple[str, ...] = ()
    sudo_token: str | None = None
    sudo_expires_at: str | None = None  # ISO 8601, parsed where it's used


def config_dir() -> Path:
    """`$XDG_CONFIG_HOME/ansina`, else `~/.config/ansina`."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "ansina"


def load_config() -> Config:
    path = config_dir() / _CONFIG_FILE
    if not path.exists():
        return Config()
    data = tomllib.loads(path.read_text())
    default_host = data.get("default_host")
    return Config(default_host=default_host if isinstance(default_host, str) else None)


def load_hosts() -> dict[str, HostEntry]:
    """Every stored host's credentials, keyed by host URL.

    Raises `InsecureCredentialsFileError` rather than silently reading a group- or
    world-readable file — it isn't automatically repaired, the fix is named instead.
    """
    path = config_dir() / _HOSTS_FILE
    if not path.exists():
        return {}

    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & _INSECURE_MODE_MASK:
        raise InsecureCredentialsFileError(path, mode)

    data = tomllib.loads(path.read_text())
    hosts = data.get("hosts", {})
    if not isinstance(hosts, dict):
        return {}
    return {host: _host_entry_from_table(table) for host, table in hosts.items()}


def save_hosts(hosts: Mapping[str, HostEntry]) -> None:
    """Write `hosts.toml` at mode 0600 — via `os.open`, and `chmod`ed again after in
    case a stale, more-permissive file already existed (`os.open`'s `mode` argument
    only applies when the file is newly created)."""
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / _HOSTS_FILE

    content = _dump_toml(hosts)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
    finally:
        os.chmod(path, 0o600)


def resolve_host(cli_host: str | None, env: Mapping[str, str], config: Config) -> str:
    """`--host` > `ANSINA_HOST` > `config.toml`'s `default_host` > the built-in
    default — matching the daemon's own default bind (`ServerSettings.host`)."""
    return cli_host or env.get("ANSINA_HOST") or config.default_host or DEFAULT_HOST


def resolve_token(
    host: str, hosts: Mapping[str, HostEntry], env: Mapping[str, str]
) -> str | None:
    """`ANSINA_TOKEN` > the stored token for `host`, if any."""
    env_token = env.get("ANSINA_TOKEN")
    if env_token:
        return env_token
    entry = hosts.get(host)
    return entry.token if entry else None


def _host_entry_from_table(table: object) -> HostEntry:
    if not isinstance(table, dict):
        return HostEntry()
    roles = table.get("roles", [])
    token = table.get("token")
    username = table.get("username")
    sudo_token = table.get("sudo_token")
    sudo_expires_at = table.get("sudo_expires_at")
    return HostEntry(
        token=token if isinstance(token, str) else None,
        username=username if isinstance(username, str) else None,
        roles=tuple(roles) if isinstance(roles, list) else (),
        sudo_token=sudo_token if isinstance(sudo_token, str) else None,
        sudo_expires_at=sudo_expires_at if isinstance(sudo_expires_at, str) else None,
    )


def _dump_toml(hosts: Mapping[str, HostEntry]) -> str:
    """Serialize `hosts` as `[hosts."<host>"]` tables. Handles exactly the field
    types `HostEntry` can hold (`str | None`, `tuple[str, ...]`) — nothing more
    general is needed, and nothing more general is attempted."""
    lines: list[str] = []
    for host, entry in hosts.items():
        lines.append(f"[hosts.{_toml_string(host)}]")
        for name, value in (
            ("token", entry.token),
            ("username", entry.username),
            ("sudo_token", entry.sudo_token),
            ("sudo_expires_at", entry.sudo_expires_at),
        ):
            if value is not None:
                lines.append(f"{name} = {_toml_string(value)}")
        if entry.roles:
            roles = ", ".join(_toml_string(role) for role in entry.roles)
            lines.append(f"roles = [{roles}]")
        lines.append("")
    return "\n".join(lines)


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
