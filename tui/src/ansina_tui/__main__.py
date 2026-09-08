"""`python -m ansina_tui` entry point — resolves to the same Typer app as the
`ansina-tui` console script (`[project.scripts]`).
"""

from __future__ import annotations

from ansina_tui.main import app as app  # explicit re-export, mypy --strict

if __name__ == "__main__":
    app()
