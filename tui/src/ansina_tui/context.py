"""`AppContext` — the global options every command reads, resolved once in `main.py`'s
root callback and stashed on `typer.Context.obj`. Its own module so `commands/*.py`
can import it without reaching back into `main.py` (which imports `commands/*.py` to
register them) and creating a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AppContext:
    """`--host`/`--json`/`--verbose` as parsed at the root, before any subcommand."""

    host: str | None
    json_output: bool
    verbose: bool
