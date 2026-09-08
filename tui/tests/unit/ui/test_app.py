from __future__ import annotations

from textual.widgets import Static

from ansina_tui.config import DEFAULT_HOST
from ansina_tui.ui.app import AnsinaTuiApp


async def test_app_shows_the_given_host() -> None:
    app = AnsinaTuiApp(host="http://example:8000")
    async with app.run_test():
        static = app.query_one("#connection-status", Static)
        assert "http://example:8000" in str(static.content)


async def test_app_falls_back_to_default_host_when_none_given() -> None:
    app = AnsinaTuiApp()
    async with app.run_test():
        static = app.query_one("#connection-status", Static)
        assert DEFAULT_HOST in str(static.content)


async def test_quit_binding_exits_the_pilot_session() -> None:
    app = AnsinaTuiApp()
    async with app.run_test() as pilot:
        await pilot.press("q")
        assert app.return_code is not None
