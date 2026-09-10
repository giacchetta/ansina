"""The contract every tab in the shell implements (issue #34).

`AnsinaTuiApp` composes `TABS` generically and dispatches `r` via
`self.query(RefreshableTab)` — Textual's widget query matches subclasses, so adding a
tab later is an append to `app.py`'s `TABS` tuple, never a change to its structure.
"""

from __future__ import annotations

from typing import ClassVar

from textual.containers import VerticalScroll

from ansina_tui.context import AppContext


class RefreshableTab(VerticalScroll):
    """Base class for every tab: a stable id/title pair, the shared `AppContext`
    (the only cross-cutting state a tab needs — `--host`/`--json`/`--verbose` are
    CLI-only and don't apply to the TUI; `--refresh` is what an Overview-style tab
    reads), and an on-demand refresh.

    `__init__`'s signature is the contract `app.py`'s `tab_cls(self._app_context)`
    relies on: every subclass must accept exactly `app_context` positionally (extra
    keyword-only parameters, like `OverviewTab`'s injectable `fetcher`, are fine).
    """

    TAB_ID: ClassVar[str] = ""
    TAB_TITLE: ClassVar[str] = ""

    def __init__(self, app_context: AppContext) -> None:
        super().__init__(id=f"{self.TAB_ID}-tab")
        self._app_context = app_context

    def request_refresh(self) -> None:
        """Trigger an immediate refresh. The base implementation is a no-op so
        `AnsinaTuiApp.action_refresh` can call it uniformly across every tab even
        before a subclass overrides it."""
