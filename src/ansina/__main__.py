"""`python -m ansina` / `ansina` console script — the process entry point (issue #4).

Boot sequence, in order: parse argv, load config, configure logging (so uvicorn's own
log lines come out as redacted JSON too), build the app, hand it to uvicorn — wrapped
in `ansina.dev.dev_mode` (issue #62), which is a byte-for-byte no-op unless `--dev` was
passed. `configure_logging` is deliberately called here and nowhere in `ansina.api` —
`ansina.logging`'s own docstring names this as the one boot call site, so importing
`ansina.api.app` for tests never has the side effect of reconfiguring the root logger.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

import uvicorn

from ansina.api import create_app
from ansina.config import ConfigError, load_settings
from ansina.dev import dev_mode
from ansina.errors import AnsinaError
from ansina.logging import configure_logging, get_logger


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """`--dev` is the only flag (issue #62) — `ansina --dev` establishes Dev Mode's
    lab/pre-customer posture. `argv=None` (the production default) reads
    `sys.argv[1:]`; the unit suite passes an explicit list so a test never depends
    on how it itself was invoked.
    """
    parser = argparse.ArgumentParser(prog="ansina")
    parser.add_argument(
        "--dev",
        action="store_true",
        help=(
            "Enable Dev Mode: a lab/pre-customer posture that preflights and "
            "supervises a Vector telemetry sidecar. Never use in production."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)

    try:
        settings = load_settings(
            overrides={"dev": {"enabled": True}} if args.dev else None
        )
    except ConfigError as exc:
        # No logger yet — logging isn't configured until settings load successfully —
        # so this goes straight to stderr rather than through `get_logger`.
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    configure_logging(settings)
    logger = get_logger(__name__)
    logger.info(
        "starting ansina",
        extra={"host": settings.server.host, "port": settings.server.port},
    )

    try:
        app = create_app(settings)
    except AnsinaError as exc:
        # Mirrors the `ConfigError` branch above: `create_app` can now fail before
        # uvicorn ever binds (issue #10's Heart capability probe) — same clean
        # stderr-and-exit-1 shape, never a traceback.
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    with dev_mode(settings):
        uvicorn.run(
            app,
            host=settings.server.host,
            port=settings.server.port,
            log_config=None,
        )


if __name__ == "__main__":
    main()
