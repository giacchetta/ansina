"""Pins issue #32's own acceptance criterion: a token or password is never readable
as a command-line argument or option value — only `--with-token`'s own boolean flag,
a no-echo prompt, or stdin carries one. Walks the whole Click command tree (vendored
inside Typer as of 0.27, `typer._click` — see `main.py`'s own module docstring) rather
than hand-listing each command, so a future command is covered automatically.
"""

from __future__ import annotations

import typer._click.core as click_core  # the vendored base `.commands.items()` yields
import typer.core
import typer.main

from ansina_tui.main import app

_SECRET_NAME_FRAGMENTS = ("token", "password", "secret", "pw")

# Deliberately not secrets, despite the name match:
# - `with_token`: a boolean flag (`auth login --with-token` reads the token from
#   stdin, never argv) — filtered by the `is_flag` check below anyway, listed here
#   for clarity.
# - `token_id`: a token's opaque *id* (`auth token revoke <token_id>`, `auth token
#   list`'s own "id" column) — a public identifier, never the secret token value
#   itself.
_ALLOWED_VALUE_TAKING_NAMES = {"with_token", "token_id"}


def _walk(
    group: typer.core.TyperGroup, prefix: tuple[str, ...] = ()
) -> list[tuple[tuple[str, ...], click_core.Command]]:
    found: list[tuple[tuple[str, ...], click_core.Command]] = []
    for name, command in group.commands.items():
        path = (*prefix, name)
        found.append((path, command))
        if isinstance(command, typer.core.TyperGroup):
            found.extend(_walk(command, path))
    return found


def test_no_value_taking_parameter_is_named_like_a_secret() -> None:
    root = typer.main.get_command(app)
    assert isinstance(root, typer.core.TyperGroup)

    offenders: list[str] = []
    for path, command in _walk(root):
        for param in command.params:
            if isinstance(param, typer.core.TyperOption) and param.is_flag:
                continue  # a boolean flag never carries a value
            name = param.name or ""
            if name in _ALLOWED_VALUE_TAKING_NAMES:
                continue
            if any(fragment in name.lower() for fragment in _SECRET_NAME_FRAGMENTS):
                offenders.append(f"{' '.join(('ansina-tui', *path))}: {name}")

    assert offenders == [], f"value-taking parameters named like a secret: {offenders}"
