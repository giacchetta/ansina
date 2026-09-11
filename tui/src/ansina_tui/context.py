"""`AppContext` — the global options every command reads, resolved once in `main.py`'s
root callback and stashed on `typer.Context.obj`. Its own module so `commands/*.py`
can import it without reaching back into `main.py` (which imports `commands/*.py` to
register them) and creating a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AppContext:
    """`--host`/`--json`/`--verbose` as parsed at the root, before any subcommand.

    `refresh` (issue #34) is TUI-only — the Overview tab's refresh interval in
    seconds — but lives here rather than a TUI-specific context because the no-args
    rule means the TUI has no subcommand of its own to carry an option: `--refresh`
    can only ever be a root-level flag, resolved once alongside the other three.
    Every CLI subcommand ignores it. Defaulted so every existing construction site
    (`main.py`'s root callback, and every test that builds one directly) is
    unaffected.
    """

    host: str | None
    json_output: bool
    verbose: bool
    refresh: float = 5.0
