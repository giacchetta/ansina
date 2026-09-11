"""Pins `RefreshableTab`'s contract: it stores the shared `AppContext`, builds a
stable `f"{TAB_ID}-tab"` widget id, and `request_refresh` is a safe no-op until a
subclass overrides it — the base `AnsinaTuiApp.action_refresh` relies on calling it
uniformly across every tab."""

from __future__ import annotations

from typing import ClassVar

from ansina_tui.context import AppContext
from ansina_tui.ui.tabs import RefreshableTab


class _StubTab(RefreshableTab):
    TAB_ID: ClassVar[str] = "stub"
    TAB_TITLE: ClassVar[str] = "Stub"


def _ctx() -> AppContext:
    return AppContext(host="http://x", json_output=False, verbose=False)


def test_stores_the_app_context() -> None:
    context = _ctx()
    tab = _StubTab(context)
    assert tab._app_context is context


def test_widget_id_is_derived_from_tab_id() -> None:
    tab = _StubTab(_ctx())
    assert tab.id == "stub-tab"


def test_base_request_refresh_is_a_no_op() -> None:
    tab = _StubTab(_ctx())
    tab.request_refresh()  # must not raise
