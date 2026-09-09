"""Secret input: a token or password never arrives via argv or a flag value — only a
no-echo prompt or stdin. Its own module because issue #32 pins this as its own
acceptance criterion, verified by `tests/unit/test_secret_input.py` and, at the
command-tree level, by `tests/unit/commands/auth/test_login.py`'s walk of every Click
parameter `main.app` exposes.

Mirrors `gh auth login --with-token`'s convention: the flag (or a stdin that isn't a
TTY at all, detected automatically — a piped invocation with no flag) reads the secret
from stdin; otherwise `typer.prompt(hide_input=True)` asks for it directly, so nothing
ever needs to be typed in the clear or appear in `ps`.
"""

from __future__ import annotations

import sys
from typing import TextIO

import typer


class SecretInputError(Exception):
    """The secret resolved to nothing — an empty stdin read or an empty prompt
    answer. Mapped to `ExitCode.USAGE` by every caller."""

    def __init__(self, what: str) -> None:
        self.what = what
        super().__init__(f"{what} was empty")


def _read_line(stream: TextIO) -> str:
    return stream.readline().strip()


def read_token(*, with_token: bool, stream: TextIO | None = None) -> str:
    """The API token for `auth login`. `with_token` is `--with-token`; a piped
    (non-TTY) stdin is honored the same way even without the flag.

    `stream` defaults to `None`, resolved to `sys.stdin` *inside* the function body
    rather than as a `= sys.stdin` default-argument value — a default argument is
    bound once, at module-import time, so it would keep pointing at whatever
    `sys.stdin` was at import time even after `typer.testing.CliRunner` swaps
    `sys.stdin` for the duration of an `invoke()` call (the same trap `main.py`'s
    `_is_interactive` docstring already documents).
    """
    stream = stream if stream is not None else sys.stdin
    if with_token or not stream.isatty():
        value = _read_line(stream)
    else:
        value = typer.prompt("Token", hide_input=True)
    if not value:
        raise SecretInputError("token")
    return value


def read_password(*, prompt: str = "Password", stream: TextIO | None = None) -> str:
    """The step-up password for `auth sudo`. A piped (non-TTY) stdin is read
    automatically; otherwise a no-echo prompt. See `read_token` for why `stream`
    resolves to `sys.stdin` in the body, not as a default-argument value."""
    stream = stream if stream is not None else sys.stdin
    if not stream.isatty():
        value = _read_line(stream)
    else:
        value = typer.prompt(prompt, hide_input=True)
    if not value:
        raise SecretInputError("password")
    return value
