"""Shared "print this Typer app's help to stderr, then exit usage" helper — the root
`main.py` callback needed this first (bare invocation on a non-TTY), and `commands/
auth/__init__.py`'s two command-group callbacks (issue #32: bare `auth`, bare `auth
token`) need the exact same thing, so it lives here instead of being copied twice.

`ctx.get_help()`'s return value is not the whole story: Typer's rich-formatted help
(`typer.rich_utils.rich_format_help`) builds its own `Console` and prints through it
as a side effect of formatting, and that `Console` resolves `sys.stdout` dynamically
at print time when no file was bound explicitly — so
`typer.echo(ctx.get_help(), err=True)` alone doesn't route it to stderr. Swapping
`sys.stdout` for the call's duration redirects the same rich-styled output to stderr
instead.
"""

from __future__ import annotations

import sys

import typer


def print_help_to_stderr(ctx: typer.Context) -> None:
    original_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        ctx.get_help()
    finally:
        sys.stdout = original_stdout
