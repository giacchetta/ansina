"""The `ansina-tui auth` command group: login, status, logout, sudo, and the nested
`token mint|list|revoke`. See issue #32.
"""

from __future__ import annotations

import typer

from ansina_tui.clihelp import print_help_to_stderr
from ansina_tui.commands.auth.login import login_command, logout_command
from ansina_tui.commands.auth.status import auth_status_command
from ansina_tui.commands.auth.sudo import sudo_command
from ansina_tui.commands.auth.tokens import list_command, mint_command, revoke_command
from ansina_tui.exits import ExitCode

auth_app = typer.Typer(
    name="auth",
    no_args_is_help=False,
    invoke_without_command=True,
    help="Store a credential, see who you are, and manage tokens/sudo.",
)
token_app = typer.Typer(
    name="token",
    no_args_is_help=False,
    invoke_without_command=True,
    help="Mint, list, and revoke your own API tokens.",
)


def _require_subcommand(ctx: typer.Context) -> None:
    """Bare `auth`/`auth token` is a usage error, not silent success — the same
    `ExitCode.USAGE` the root app uses for its own bare-non-TTY case."""
    if ctx.resilient_parsing or ctx.invoked_subcommand is not None:
        return
    print_help_to_stderr(ctx)
    raise typer.Exit(ExitCode.USAGE)


@auth_app.callback(invoke_without_command=True)
def _auth_main(ctx: typer.Context) -> None:
    _require_subcommand(ctx)


@token_app.callback(invoke_without_command=True)
def _token_main(ctx: typer.Context) -> None:
    _require_subcommand(ctx)


auth_app.command("login")(login_command)
auth_app.command("logout")(logout_command)
auth_app.command("status")(auth_status_command)
auth_app.command("sudo")(sudo_command)
token_app.command("mint")(mint_command)
token_app.command("list")(list_command)
token_app.command("revoke")(revoke_command)
auth_app.add_typer(token_app, name="token")
