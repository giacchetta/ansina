"""Output discipline: human-readable tables by default; with `--json`, stdout carries
JSON and **nothing else** — every diagnostic goes to stderr, so the CLI is safe to
pipe. JSON is written with a plain `sys.stdout.write`, never through `rich`, so
terminal-width wrapping or color codes can never leak into a piped payload.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from rich.console import Console
from rich.table import Table


class Emitter:
    """The one object every command uses to talk to the terminal."""

    def __init__(self, *, json_mode: bool = False, verbose: bool = False) -> None:
        self.json_mode = json_mode
        self.verbose = verbose
        self._stdout = Console(file=sys.stdout, highlight=False)
        self._stderr = Console(file=sys.stderr, stderr=True, highlight=False)

    def rows(self, rows: Sequence[tuple[str, str]]) -> None:
        """A borderless two-column label/value table. Suppressed entirely in JSON
        mode — `--json` output comes only from `Emitter.json`."""
        if self.json_mode:
            return
        table = Table(show_header=False, box=None, pad_edge=False)
        table.add_column(no_wrap=True)
        table.add_column()
        for label, value in rows:
            table.add_row(label, value)
        self._stdout.print(table)

    def table(self, columns: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
        """A headered multi-column table (`auth token list`). Suppressed in JSON
        mode — same discipline as `rows`."""
        if self.json_mode:
            return
        rich_table = Table(show_header=True, header_style="bold")
        for column in columns:
            rich_table.add_column(column)
        for row in rows:
            rich_table.add_row(*row)
        self._stdout.print(rich_table)

    def line(self, text: str) -> None:
        """One line of plain stdout output, suppressed in JSON mode."""
        if self.json_mode:
            return
        self._stdout.print(text)

    def body(self, text: str) -> None:
        """A response body verbatim (`commands/api.py`), on stdout, **never**
        suppressed by JSON mode — this *is* the JSON-mode payload for `api`, and stays
        the plain-mode payload too — and never through `rich`, so a piped payload can't
        pick up line-wrapping or color codes. An empty string writes nothing at all
        (a 204/empty body), matching `emitter.json`'s always-write-something contrast:
        `body` mirrors whatever the daemon actually sent, including sending nothing."""
        if not text:
            return
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()

    def json(self, data: Mapping[str, Any]) -> None:
        """The single JSON payload for `--json` mode. Writes directly to
        `sys.stdout`, bypassing `rich` entirely."""
        sys.stdout.write(json.dumps(data, indent=2, sort_keys=True))
        sys.stdout.write("\n")
        sys.stdout.flush()

    def error(self, message: str) -> None:
        """A diagnostic. Always goes to stderr, JSON mode or not — that's the whole
        point of the discipline: stdout is reserved for the JSON payload."""
        self._stderr.print(f"[bold red]error:[/bold red] {message}")

    def warn(self, message: str) -> None:
        """A non-fatal diagnostic (e.g. `logout`'s local-only caveat). Stderr,
        always — same reasoning as `error`."""
        self._stderr.print(f"[bold yellow]warning:[/bold yellow] {message}")

    def debug(self, text: str) -> None:
        """`--verbose`-only diagnostic detail (e.g. a request id). Never carries a
        secret — see `tests/unit/commands/auth/` for the pinning tests that check
        this across every auth command."""
        if self.verbose:
            self._stderr.print(f"[dim]{text}[/dim]")
