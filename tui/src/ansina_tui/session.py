"""The credential/session resolution layer above `config.py`: one place that loads
`config.toml` + `hosts.toml`, resolves `--host`/`ANSINA_HOST`/`ANSINA_TOKEN`
precedence, and builds the `ApiClient` every command talks through.

Introduced by issue #32 so `auth`'s six commands and `commands/status.py` (moved onto
it here) share one resolution path and one test seam (`build_client`), instead of
`status.py`'s own inline block from #31.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import httpx

from ansina_tui.client import ApiClient, parse_expiry
from ansina_tui.config import (
    Config,
    HostEntry,
    load_config,
    load_hosts,
    resolve_host,
    resolve_token,
    save_config,
    save_hosts,
)
from ansina_tui.context import AppContext


def _default_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Session:
    """Resolved host + credential state for one command invocation.

    `entry` is the stored `HostEntry` for `host` with any *expired* sudo grant
    already stripped in memory — the one `build_client` (and anything that re-saves
    an entry unchanged) reads from, so an expired grant can never reach the wire or
    get written back as if it were still live. `hosts` is the full, unscrubbed table
    as loaded, for the one caller (`auth sudo --status`) that needs to tell "no grant
    was ever stored" apart from "one was stored and has since expired".
    """

    host: str
    config: Config
    hosts: dict[str, HostEntry]
    entry: HostEntry
    token: str | None
    token_from_env: bool


def resolve_session(
    app_context: AppContext, *, env: Mapping[str, str], now: datetime | None = None
) -> Session:
    """Load `config.toml`/`hosts.toml` and resolve host/token for this invocation.

    Propagates `InsecureCredentialsFileError` unchanged (from `load_hosts`) — every
    command handles it the same way `commands/status.py` already did under #31.
    """
    config = load_config()
    hosts = load_hosts()
    host = resolve_host(app_context.host, env, config)
    raw_entry = hosts.get(host, HostEntry())
    entry = _without_expired_grant(raw_entry, now or _default_now())

    return Session(
        host=host,
        config=config,
        hosts=dict(hosts),
        entry=entry,
        token=resolve_token(host, hosts, env),
        token_from_env=bool(env.get("ANSINA_TOKEN")),
    )


def _without_expired_grant(entry: HostEntry, now: datetime) -> HostEntry:
    if entry.sudo_token is None:
        return entry
    expiry = parse_expiry(entry.sudo_expires_at)
    if expiry is not None and expiry > now:
        return entry
    return replace(entry, sudo_token=None, sudo_expires_at=None)


def build_client(
    session: Session,
    *,
    transport: httpx.BaseTransport | None = None,
    now: Callable[[], datetime] = _default_now,
    token: str | None = None,
) -> ApiClient:
    """The one `ApiClient` construction point — and the one seam
    `tests/conftest.py`'s `patch_transport` fixture patches.

    `token`, when given, overrides `session.token` — `auth login` uses this to
    authenticate with the just-supplied token before anything is stored, never the
    session's existing (old, or absent) one.
    """
    return ApiClient(
        session.host,
        token=token if token is not None else session.token,
        sudo_token=session.entry.sudo_token,
        sudo_expires_at=session.entry.sudo_expires_at,
        now=now,
        transport=transport,
    )


def save_entry(session: Session, entry: HostEntry) -> None:
    """Persist `entry` for `session.host`. The first successful login (no
    `default_host` set yet) also becomes the default host."""
    hosts = dict(session.hosts)
    hosts[session.host] = entry
    save_hosts(hosts)
    if session.config.default_host is None:
        save_config(replace(session.config, default_host=session.host))


def remove_entry(session: Session) -> None:
    """Drop `session.host`'s entry entirely (`auth logout`). Idempotent — removing a
    host with no stored entry is a no-op."""
    hosts = dict(session.hosts)
    hosts.pop(session.host, None)
    save_hosts(hosts)


@dataclass(frozen=True, slots=True)
class SudoStatus:
    """`none`: no grant is stored. `expired`: one is on disk but its `expires_at` has
    passed — informational only, never attached to a request. `active`: live;
    `remaining` says how much longer."""

    state: str  # "none" | "expired" | "active"
    expires_at: datetime | None
    remaining: timedelta | None


def sudo_state(entry: HostEntry, now: datetime) -> SudoStatus:
    """Read `entry`'s raw sudo fields (pass `session.hosts[session.host]`, not the
    already-scrubbed `session.entry`, to see an `expired` grant rather than `none`)."""
    if entry.sudo_token is None:
        return SudoStatus(state="none", expires_at=None, remaining=None)
    expiry = parse_expiry(entry.sudo_expires_at)
    if expiry is None or expiry <= now:
        return SudoStatus(state="expired", expires_at=expiry, remaining=None)
    return SudoStatus(state="active", expires_at=expiry, remaining=expiry - now)
