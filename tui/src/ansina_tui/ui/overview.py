"""Overview tab (issue #34): host, `/healthz`, `/readyz` per-check rows, `/version`,
and the caller's identity from `GET /auth/me` — refreshed on a timer (`--refresh`
seconds, default 5) and immediately on `r`.

Every network call goes through `daemon_state.fetch_overview`, which never raises —
see its module docstring. `_load` runs it in a thread (`asyncio.to_thread`) inside an
`exclusive=True` worker, the same anti-overlap guard the daemon's own `TickLoop` uses,
so a slow refresh can never overlap the next tick. Existing widgets are updated in
place on every refresh — never re-mounted — which is what keeps the tab flicker-free
and duplication-free across ticks.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import ClassVar

from rich.table import Table
from textual import work
from textual.app import ComposeResult
from textual.widgets import Static

from ansina_tui.context import AppContext
from ansina_tui.daemon_state import OverviewSnapshot, Panel, fetch_overview
from ansina_tui.ui.tabs import RefreshableTab

_EMPTY_PLACEHOLDER = "—"


def _render_panel(panel: Panel) -> Table | str:
    """A panel with rows renders as a borderless label/value table (rows win when a
    panel carries both, e.g. `/readyz`'s failing checks alongside its detail message —
    the checks are the useful part); a message-only panel renders as plain text; an
    empty panel (a whole-tab failure — see `daemon_state._EMPTY_PANEL`) renders as a
    placeholder dash rather than nothing at all."""
    if panel.rows:
        table = Table(show_header=False, box=None, pad_edge=False)
        table.add_column(no_wrap=True)
        table.add_column()
        for label, value in panel.rows:
            table.add_row(label, value)
        return table
    return panel.message or _EMPTY_PLACEHOLDER


class OverviewTab(RefreshableTab):
    """The one tab M4 ships. `fetcher` is injectable so widget tests can drive every
    designed state directly, with no transport involved."""

    TAB_ID: ClassVar[str] = "overview"
    TAB_TITLE: ClassVar[str] = "Overview"

    def __init__(
        self,
        app_context: AppContext,
        *,
        fetcher: Callable[[], OverviewSnapshot] | None = None,
    ) -> None:
        super().__init__(app_context)
        self._fetcher = fetcher or self._fetch_from_daemon

    def _fetch_from_daemon(self) -> OverviewSnapshot:
        return fetch_overview(self._app_context, env=os.environ)

    def compose(self) -> ComposeResult:
        yield Static(id="overview-connection")
        yield Static("Health", classes="panel-title")
        yield Static(id="overview-health")
        yield Static("Readiness", classes="panel-title")
        yield Static(id="overview-readiness")
        yield Static("Version", classes="panel-title")
        yield Static(id="overview-version")
        yield Static("Identity", classes="panel-title")
        yield Static(id="overview-identity")

    def on_mount(self) -> None:
        self.request_refresh()
        self.set_interval(self._app_context.refresh, self.request_refresh)

    def request_refresh(self) -> None:
        self._load()

    @work(exclusive=True, group="overview")
    async def _load(self) -> None:
        snapshot = await asyncio.to_thread(self._fetcher)
        self._apply_snapshot(snapshot)

    def _apply_snapshot(self, snapshot: OverviewSnapshot) -> None:
        connection_line = f"{snapshot.host} · {snapshot.connection}"
        display_line = (
            f"{connection_line}\n{snapshot.error}"
            if snapshot.error
            else connection_line
        )
        self.query_one("#overview-connection", Static).update(display_line)
        self.query_one("#overview-health", Static).update(
            _render_panel(snapshot.health)
        )
        self.query_one("#overview-readiness", Static).update(
            _render_panel(snapshot.readiness)
        )
        self.query_one("#overview-version", Static).update(
            _render_panel(snapshot.version)
        )
        self.query_one("#overview-identity", Static).update(
            _render_panel(snapshot.identity)
        )
        self.app.sub_title = connection_line
