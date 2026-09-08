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

    def __init__(self, *, json_mode: bool = False) -> None:
        self.json_mode = json_mode
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

    def line(self, text: str) -> None:
        """One line of plain stdout output, suppressed in JSON mode."""
        if self.json_mode:
            return
        self._stdout.print(text)

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
