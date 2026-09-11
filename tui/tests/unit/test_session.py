"""Pins `session.py`'s credential-resolution contract: an expired sudo grant is
scrubbed in memory before a client is ever built (and never sent), `save_entry` sets
the default host only once, and `sudo_state` distinguishes none/expired/active."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from ansina_tui.config import (
    Config,
    HostEntry,
    load_config,
    load_hosts,
    save_config,
    save_hosts,
)
from ansina_tui.context import AppContext
from ansina_tui.session import (
    build_client,
    remove_entry,
    resolve_session,
    save_entry,
    sudo_state,
)


def _ctx(host: str | None) -> AppContext:
    return AppContext(host=host, json_output=False, verbose=False)


def test_resolve_session_picks_up_stored_token(tmp_xdg_home: Path) -> None:
    save_hosts({"http://x": HostEntry(token="stored-token")})

    session = resolve_session(_ctx("http://x"), env={})

    assert session.token == "stored-token"
    assert session.token_from_env is False


def test_resolve_session_env_token_wins_and_is_flagged(tmp_xdg_home: Path) -> None:
    save_hosts({"http://x": HostEntry(token="stored-token")})

    session = resolve_session(_ctx("http://x"), env={"ANSINA_TOKEN": "env-token"})

    assert session.token == "env-token"
    assert session.token_from_env is True


def test_resolve_session_scrubs_an_expired_grant(
    tmp_xdg_home: Path, frozen_now: datetime, iso: Callable[[timedelta], str]
) -> None:
    save_hosts(
        {
            "http://x": HostEntry(
                token="t",
                sudo_token="grant",
                sudo_expires_at=iso(timedelta(minutes=-5)),
            )
        }
    )

    session = resolve_session(_ctx("http://x"), env={}, now=frozen_now)

    assert session.entry.sudo_token is None
    assert session.entry.sudo_expires_at is None
    # The raw table is untouched, so a caller can still tell "expired" from "none".
    assert session.hosts["http://x"].sudo_token == "grant"


def test_resolve_session_keeps_a_live_grant(
    tmp_xdg_home: Path, frozen_now: datetime, iso: Callable[[timedelta], str]
) -> None:
    save_hosts(
        {
            "http://x": HostEntry(
                token="t",
                sudo_token="grant",
                sudo_expires_at=iso(timedelta(minutes=5)),
            )
        }
    )

    session = resolve_session(_ctx("http://x"), env={}, now=frozen_now)

    assert session.entry.sudo_token == "grant"


def test_build_client_never_sends_an_expired_grant(
    tmp_xdg_home: Path,
    frozen_now: datetime,
    iso: Callable[[timedelta], str],
    captured_requests: list[httpx.Request],
    capturing_transport: Callable[[httpx.BaseTransport], httpx.MockTransport],
) -> None:
    save_hosts(
        {
            "http://x": HostEntry(
                token="t",
                sudo_token="grant",
                sudo_expires_at=iso(timedelta(minutes=-5)),
            )
        }
    )
    session = resolve_session(_ctx("http://x"), env={}, now=frozen_now)
    inner = httpx.MockTransport(lambda request: httpx.Response(200, json={}))

    with build_client(session, transport=capturing_transport(inner)) as client:
        client.get("/healthz")

    assert "x-sudo-token" not in captured_requests[0].headers


def test_build_client_token_override_beats_the_stored_one(tmp_xdg_home: Path) -> None:
    save_hosts({"http://x": HostEntry(token="old-token")})
    session = resolve_session(_ctx("http://x"), env={})
    seen_auth: list[str | None] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("authorization"))
        return httpx.Response(200, json={})

    with build_client(
        session, transport=httpx.MockTransport(_handler), token="new-token"
    ) as client:
        client.get("/healthz")

    assert seen_auth == ["Bearer new-token"]


def test_save_entry_sets_default_host_on_first_login(tmp_xdg_home: Path) -> None:
    session = resolve_session(_ctx("http://x"), env={})

    save_entry(session, HostEntry(token="t"))

    assert load_config().default_host == "http://x"
    assert load_hosts()["http://x"].token == "t"


def test_save_entry_does_not_override_an_existing_default_host(
    tmp_xdg_home: Path,
) -> None:
    save_config(Config(default_host="http://first"))
    session = resolve_session(_ctx("http://second"), env={})

    save_entry(session, HostEntry(token="t"))

    assert load_config().default_host == "http://first"


def test_remove_entry_drops_the_host(tmp_xdg_home: Path) -> None:
    save_hosts({"http://x": HostEntry(token="t")})
    session = resolve_session(_ctx("http://x"), env={})

    remove_entry(session)

    assert "http://x" not in load_hosts()


def test_remove_entry_is_idempotent_for_a_host_with_no_entry(
    tmp_xdg_home: Path,
) -> None:
    session = resolve_session(_ctx("http://x"), env={})

    remove_entry(session)

    assert load_hosts() == {}


def test_sudo_state_none_when_never_stored(frozen_now: datetime) -> None:
    status = sudo_state(HostEntry(), frozen_now)
    assert status.state == "none"
    assert status.expires_at is None
    assert status.remaining is None


def test_sudo_state_expired(
    frozen_now: datetime, iso: Callable[[timedelta], str]
) -> None:
    entry = HostEntry(sudo_token="g", sudo_expires_at=iso(timedelta(minutes=-1)))

    status = sudo_state(entry, frozen_now)

    assert status.state == "expired"
    assert status.remaining is None
    assert status.expires_at is not None


def test_sudo_state_active_with_remaining(
    frozen_now: datetime, iso: Callable[[timedelta], str]
) -> None:
    entry = HostEntry(sudo_token="g", sudo_expires_at=iso(timedelta(minutes=10)))

    status = sudo_state(entry, frozen_now)

    assert status.state == "active"
    assert status.remaining == timedelta(minutes=10)


def test_sudo_state_treats_a_malformed_expiry_as_expired(frozen_now: datetime) -> None:
    entry = HostEntry(sudo_token="g", sudo_expires_at="not-a-date")

    status = sudo_state(entry, frozen_now)

    assert status.state == "expired"
    assert status.expires_at is None
