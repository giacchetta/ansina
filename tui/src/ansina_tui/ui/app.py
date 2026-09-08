"""The Textual TUI shell.

This issue (#31) ships only a placeholder — header, footer, a "connected to <host>"
line. Issue #34 replaces the body with the real Overview tab (host status, `/healthz`,
`/readyz` per-check rows, `/version`, and the caller's identity from `GET /auth/me`) on
a refresh timer; the shell itself is built so later milestones add *tabs*, not a
second application.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.widgets import Footer, Header, Static

from ansina_tui.config import DEFAULT_HOST


class AnsinaTuiApp(App[None]):
    """Bare `ansina-tui`'s entry point."""

    TITLE = "ansina-tui"
    BINDINGS: ClassVar[list[BindingType]] = [("q", "quit", "Quit")]

    def __init__(self, host: str | None = None) -> None:
        super().__init__()
        self.host = host or DEFAULT_HOST

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(f"Connected to {self.host}", id="connection-status")
        yield Footer()
