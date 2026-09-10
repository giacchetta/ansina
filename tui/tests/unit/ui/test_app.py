"""Pins the shell's contract: `TABS` composes into a `TabbedContent` with the first
tab focused, `r` dispatches a refresh to every `RefreshableTab` uniformly, and `q`
exits cleanly. Issue #34 replaces the placeholder body #31 shipped with the real
Overview tab; this module now drives the whole `AnsinaTuiApp` end to end with a
fake, injectable-fetcher tab standing in for `OverviewTab` where the test cares only
about the shell's own composition/dispatch, not Overview's own rendering (covered by
`tests/unit/ui/test_overview.py`).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

import httpx
from textual.app import ComposeResult
from textual.widgets import Static, TabbedContent

from ansina_tui.context import AppContext
from ansina_tui.ui.app import AnsinaTuiApp
from ansina_tui.ui.overview import OverviewTab
from ansina_tui.ui.tabs import RefreshableTab


def _ctx() -> AppContext:
    return AppContext(host="http://example:8000", json_output=False, verbose=False)


class _CountingTab(RefreshableTab):
    """A minimal `RefreshableTab` that records how many times it was asked to
    refresh, standing in for a real data-driven tab."""

    TAB_ID: ClassVar[str] = "counting"
    TAB_TITLE: ClassVar[str] = "Counting"

    def __init__(self, app_context: AppContext) -> None:
        super().__init__(app_context)
        self.refresh_calls = 0

    def compose(self) -> ComposeResult:
        yield Static("stub", id="counting-content")

    def request_refresh(self) -> None:
        self.refresh_calls += 1


class _TestApp(AnsinaTuiApp):
    """`AnsinaTuiApp` with a lightweight tab in place of the real `OverviewTab`, so
    dispatch/composition tests never make an HTTP-shaped call at all."""

    TABS: ClassVar[tuple[type[RefreshableTab], ...]] = (_CountingTab,)


async def test_overview_tab_is_present_and_focused_on_the_real_app(
    tmp_xdg_home: Path,
    unreachable_transport: httpx.MockTransport,
    patch_transport: Callable[[httpx.BaseTransport], None],
) -> None:
    # A real `OverviewTab` fetches on mount — routed through an unreachable
    # transport rather than a real socket, so this stays fast and deterministic
    # without depending on network access.
    patch_transport(unreachable_transport)

    app = AnsinaTuiApp(_ctx())
    async with app.run_test():
        await app.workers.wait_for_complete()
        tabbed = app.query_one(TabbedContent)
        assert tabbed.active == "overview"
        assert app.query_one(OverviewTab) is not None


async def test_r_binding_dispatches_refresh_to_every_tab() -> None:
    app = _TestApp(_ctx())
    async with app.run_test() as pilot:
        tab = app.query_one(_CountingTab)
        calls_before = tab.refresh_calls

        await pilot.press("r")

        assert tab.refresh_calls == calls_before + 1


async def test_action_refresh_dispatches_via_the_base_class_query() -> None:
    app = _TestApp(_ctx())
    async with app.run_test():
        tab = app.query_one(_CountingTab)

        app.action_refresh()

        assert tab.refresh_calls == 1


async def test_quit_binding_exits_the_pilot_session_cleanly() -> None:
    app = _TestApp(_ctx())
    async with app.run_test() as pilot:
        await pilot.press("q")
        assert app.return_code == 0


async def test_adding_a_tab_requires_no_change_beyond_registering_it() -> None:
    """Demonstrates the acceptance criterion directly: `_TestApp` above adds a
    second, self-contained tab type by overriding only `TABS` — `compose` and
    `action_refresh` are untouched."""
    app = _TestApp(_ctx())
    async with app.run_test():
        tabbed = app.query_one(TabbedContent)
        assert tabbed.active == "counting"
        assert app.query_one("#counting-content", Static) is not None
