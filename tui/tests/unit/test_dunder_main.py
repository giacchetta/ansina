"""`python -m ansina_tui` is just a re-export of `ansina_tui.main.app` behind an
`if __name__ == "__main__":` guard — that guard itself is excluded from the coverage
requirement (`[tool.coverage.report] exclude_also`, mirroring the root project), so
all that's left to exercise is the import itself.
"""

from __future__ import annotations


def test_dunder_main_exposes_the_same_typer_app() -> None:
    from ansina_tui import __main__
    from ansina_tui.main import app

    assert __main__.app is app
