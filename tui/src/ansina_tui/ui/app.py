"""The Textual TUI shell (issue #34).

A tabbed app built so later milestones add *tabs*, not a second application:
`TABS` is the single place tabs are registered, `compose` iterates it generically, and
`action_refresh` dispatches `r` to every tab uniformly via `self.query(RefreshableTab)`
(Textual's widget query matches subclasses) — adding a second tab is an append to
`TABS`, with no other change to this module.

Host and connection state stay visible in the header at all times via `App.sub_title`,
kept current by whichever tab last refreshed (in M4, `OverviewTab` is the only one).
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.widgets import Footer, Header, TabbedContent, TabPane

from ansina_tui.context import AppContext
from ansina_tui.ui.overview import OverviewTab
from ansina_tui.ui.tabs import RefreshableTab


class AnsinaTuiApp(App[None]):
    """Bare `ansina-tui`'s entry point."""

    # An absolute path, not a bare "app.tcss": Textual resolves a relative CSS_PATH
    # against `inspect.getfile(type(self))` — the *actual* (sub)class's module, not
    # the one that declared CSS_PATH — so a test harness subclassing this app from a
    # different directory (`tests/unit/ui/test_app.py`) would otherwise look for its
    # stylesheet there instead of here.
    CSS_PATH = Path(__file__).parent / "app.tcss"
    TITLE = "ansina-tui"
    TABS: ClassVar[tuple[type[RefreshableTab], ...]] = (OverviewTab,)
    BINDINGS: ClassVar[list[BindingType]] = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
    ]

    def __init__(self, app_context: AppContext) -> None:
        super().__init__()
        self._app_context = app_context

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial=self.TABS[0].TAB_ID):
            for tab_cls in self.TABS:
                with TabPane(tab_cls.TAB_TITLE, id=tab_cls.TAB_ID):
                    yield tab_cls(self._app_context)
        yield Footer()

    def action_refresh(self) -> None:
        for tab in self.query(RefreshableTab):
            tab.request_refresh()
