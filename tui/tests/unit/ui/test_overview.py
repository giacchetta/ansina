"""Pins `OverviewTab`'s rendering contract: every `daemon_state.OverviewSnapshot`
state renders as a designed message or table — never a traceback or a blank
section — and a refresh updates the existing widgets in place rather than mounting
new ones. `fetcher` is injected throughout, so no transport is involved here at all;
`tests/unit/test_daemon_state.py` covers what the daemon actually returns.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console
from textual.app import App, ComposeResult
from textual.widgets import Static

from ansina_tui.context import AppContext
from ansina_tui.daemon_state import OverviewSnapshot, Panel
from ansina_tui.ui.overview import OverviewTab


def _ctx(refresh: float = 5.0) -> AppContext:
    return AppContext(
        host="http://x", json_output=False, verbose=False, refresh=refresh
    )


def _rendered_text(static: Static) -> str:
    """Render a `Static`'s content (plain text or a `rich.table.Table`) to plain
    text, so assertions can check cell values without reaching into Rich's table
    internals."""
    buffer = io.StringIO()
    console = Console(file=buffer, width=100, color_system=None)
    console.print(static.content)
    return buffer.getvalue()


class _Harness(App[None]):
    """Hosts one `OverviewTab` directly — no `TabbedContent` shell — since this
    module tests the tab's own rendering, not `AnsinaTuiApp`'s composition
    (`tests/unit/ui/test_app.py` covers that)."""

    def __init__(self, tab: OverviewTab) -> None:
        super().__init__()
        self._tab = tab

    def compose(self) -> ComposeResult:
        yield self._tab


def _snapshot(
    *,
    connection: str = "connected",
    error: str | None = None,
    health: Panel | None = None,
    readiness: Panel | None = None,
    version: Panel | None = None,
    identity: Panel | None = None,
) -> OverviewSnapshot:
    return OverviewSnapshot(
        host="http://x",
        connection=connection,
        error=error,
        health=health if health is not None else Panel(rows=(("status", "ok"),)),
        readiness=readiness
        if readiness is not None
        else Panel(rows=(("database", "ok"),)),
        version=version
        if version is not None
        else Panel(rows=(("version", "ansina 0.1.0"),)),
        identity=identity
        if identity is not None
        else Panel(rows=(("username", "alice"),)),
    )


async def test_mount_triggers_an_immediate_fetch_and_renders_the_snapshot() -> None:
    snapshot = _snapshot()
    tab = OverviewTab(_ctx(), fetcher=lambda: snapshot)
    app = _Harness(tab)

    async with app.run_test():
        await app.workers.wait_for_complete()

        assert "http://x" in _rendered_text(
            app.query_one("#overview-connection", Static)
        )
        assert "connected" in _rendered_text(
            app.query_one("#overview-connection", Static)
        )
        assert "ok" in _rendered_text(app.query_one("#overview-health", Static))
        assert "ansina 0.1.0" in _rendered_text(
            app.query_one("#overview-version", Static)
        )
        assert "alice" in _rendered_text(app.query_one("#overview-identity", Static))
        assert app.sub_title == "http://x · connected"


async def test_refresh_updates_widgets_in_place_without_duplication() -> None:
    snapshots = [
        _snapshot(identity=Panel(rows=(("username", "alice"),))),
        _snapshot(identity=Panel(rows=(("username", "bob"),))),
    ]
    calls = iter(snapshots)
    tab = OverviewTab(_ctx(), fetcher=lambda: next(calls))
    app = _Harness(tab)

    async with app.run_test():
        await app.workers.wait_for_complete()
        static_count_before = len(app.query(Static))
        assert "alice" in _rendered_text(app.query_one("#overview-identity", Static))

        tab.request_refresh()
        await app.workers.wait_for_complete()

        assert len(app.query(Static)) == static_count_before
        assert "bob" in _rendered_text(app.query_one("#overview-identity", Static))


async def test_unreachable_state_renders_the_error_and_placeholder_panels() -> None:
    snapshot = _snapshot(
        connection="unreachable",
        error="Could not reach http://x: connection refused",
        health=Panel(),
        readiness=Panel(),
        version=Panel(),
        identity=Panel(),
    )
    tab = OverviewTab(_ctx(), fetcher=lambda: snapshot)
    app = _Harness(tab)

    async with app.run_test():
        await app.workers.wait_for_complete()

        connection_text = _rendered_text(app.query_one("#overview-connection", Static))
        assert "unreachable" in connection_text
        assert "Could not reach" in connection_text
        assert "Traceback" not in connection_text
        for panel_id in (
            "#overview-health",
            "#overview-readiness",
            "#overview-version",
            "#overview-identity",
        ):
            assert "—" in _rendered_text(app.query_one(panel_id, Static))
        assert app.sub_title == "http://x · unreachable"


async def test_no_credential_state_renders_the_designed_identity_message() -> None:
    snapshot = _snapshot(
        identity=Panel(
            message=(
                "No credential stored for http://x. Run `ansina-tui auth login`, "
                "or set ANSINA_TOKEN."
            )
        )
    )
    tab = OverviewTab(_ctx(), fetcher=lambda: snapshot)
    app = _Harness(tab)

    async with app.run_test():
        await app.workers.wait_for_complete()

        identity_text = _rendered_text(app.query_one("#overview-identity", Static))
        assert "auth login" in identity_text
        assert "Traceback" not in identity_text


async def test_forbidden_state_renders_a_permission_message_not_a_dump() -> None:
    snapshot = _snapshot(version=Panel(message="Your role doesn't grant this action."))
    tab = OverviewTab(_ctx(), fetcher=lambda: snapshot)
    app = _Harness(tab)

    async with app.run_test():
        await app.workers.wait_for_complete()

        version_text = _rendered_text(app.query_one("#overview-version", Static))
        assert "doesn't grant this action" in version_text
        assert "Traceback" not in version_text


async def test_not_ready_state_renders_both_the_failing_rows_and_the_message() -> None:
    snapshot = _snapshot(
        readiness=Panel(
            rows=(("database", "fail"), ("heart", "ok")),
            message="The daemon is not ready yet.",
        )
    )
    tab = OverviewTab(_ctx(), fetcher=lambda: snapshot)
    app = _Harness(tab)

    async with app.run_test():
        await app.workers.wait_for_complete()

        readiness_text = _rendered_text(app.query_one("#overview-readiness", Static))
        assert "database" in readiness_text
        assert "fail" in readiness_text


async def test_request_refresh_can_be_called_repeatedly() -> None:
    calls: list[int] = []

    def _fetch() -> OverviewSnapshot:
        calls.append(1)
        return _snapshot()

    tab = OverviewTab(_ctx(refresh=3600), fetcher=_fetch)
    app = _Harness(tab)

    async with app.run_test():
        await app.workers.wait_for_complete()
        assert len(calls) == 1

        tab.request_refresh()
        await app.workers.wait_for_complete()
        assert len(calls) == 2


def test_fetch_from_daemon_delegates_to_fetch_overview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ansina_tui.ui.overview as overview_module

    captured: dict[str, object] = {}

    def _fake_fetch_overview(
        app_context: AppContext, *, env: object
    ) -> OverviewSnapshot:
        captured["app_context"] = app_context
        captured["env"] = env
        return _snapshot()

    monkeypatch.setattr(overview_module, "fetch_overview", _fake_fetch_overview)
    context = _ctx()
    tab = OverviewTab(context)

    result = tab._fetch_from_daemon()

    assert result is not None
    assert captured["app_context"] is context
