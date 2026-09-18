"""Shared helpers across `commands/auth/*` modules. A separate module rather than
`commands/auth/__init__.py` itself — that module imports every command module to wire
them into `auth_app`, so a command module importing back from it would be a cycle.
"""

from __future__ import annotations

import typer

from ansina_tui.exits import ExitCode
from ansina_tui.output import Emitter
from ansina_tui.session import Session


def require_credential(session: Session, emitter: Emitter) -> None:
    """Exit `NOT_AUTHENTICATED` if no credential is stored for `session.host`. Shared
    by `sudo.py`, `tokens.py`, and `totp.py` — previously duplicated verbatim in each.
    """
    if session.token is None:
        emitter.error(
            f"No credential stored for {session.host}. Run `ansina-tui auth login` "
            "first."
        )
        raise typer.Exit(ExitCode.NOT_AUTHENTICATED)
