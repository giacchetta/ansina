"""The Typer app and the no-args rule — the single most important behavior in this
milestone, and the inverse of the usual default: **no arguments at all** opens the
TUI, and any argument — a subcommand, `--help`, `--version` — is the CLI.

`--help` at any level and `--version` are resolved by Click (vendored inside Typer as
of 0.27, `typer._click` — no separate `click` import) before this callback's body ever
runs (Click's own auto-added `--help`, and `--version`'s own eager option callback
below) — neither reaches `launch_tui`.
"""

from __future__ import annotations

import sys

import typer

from ansina_tui import __version__
from ansina_tui.clihelp import print_help_to_stderr
from ansina_tui.commands.api import api_command
from ansina_tui.commands.auth import auth_app
from ansina_tui.commands.status import status_command
from ansina_tui.context import AppContext
from ansina_tui.exits import ExitCode

app = typer.Typer(
    name="ansina-tui",
    no_args_is_help=False,
    invoke_without_command=True,
    add_completion=False,
    help="Ansina control surface: bare opens the TUI, any argument is the CLI.",
)


def _is_interactive() -> bool:
    """Both stdin and stdout are a TTY. Its own function (rather than inlining
    `sys.stdin.isatty()`/`sys.stdout.isatty()` in `main` below) so tests can patch it
    directly — `typer.testing.CliRunner` replaces `sys.stdin`/`sys.stdout` internally
    during `invoke()`, which would otherwise make patching the streams themselves
    from outside the call ineffective."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"ansina-tui {__version__}")
        raise typer.Exit(ExitCode.OK)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    host: str | None = typer.Option(
        None, "--host", "-H", help="Daemon base URL, e.g. http://127.0.0.1:8000."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit JSON on stdout only; diagnostics go to stderr."
    ),
    verbose: bool = typer.Option(
        False, "--verbose", help="Verbose diagnostics on stderr."
    ),
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    ctx.obj = AppContext(host=host, json_output=json_output, verbose=verbose)

    if ctx.resilient_parsing or ctx.invoked_subcommand is not None:
        return  # a subcommand (`status …`, `auth …`, later `api`), or a completion
        # probe

    if not _is_interactive():
        # Textual cannot render into a pipe. Help goes to stderr (never stdout, which
        # a caller may be piping elsewhere) so a script that typos the command gets a
        # clear error instead of a hang.
        print_help_to_stderr(ctx)
        raise typer.Exit(ExitCode.USAGE)

    launch_tui(ctx.obj)


def launch_tui(app_context: AppContext) -> None:
    from ansina_tui.ui.app import AnsinaTuiApp

    AnsinaTuiApp(host=app_context.host).run()


app.command("status")(status_command)
app.add_typer(auth_app, name="auth")
app.command("api")(api_command)


if __name__ == "__main__":
    app()
