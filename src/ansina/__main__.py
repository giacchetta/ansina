"""`python -m ansina` / `ansina` console script — the process entry point (issue #4).

Boot sequence, in order: load config, configure logging (so uvicorn's own log lines come
out as redacted JSON too), build the app, hand it to uvicorn. `configure_logging` is
deliberately called here and nowhere in `ansina.api` — `ansina.logging`'s own docstring
names this as the one boot call site, so importing `ansina.api.app` for tests never has
the side effect of reconfiguring the root logger.
"""

from __future__ import annotations

import sys

import uvicorn

from ansina.api import create_app
from ansina.config import ConfigError, load_settings
from ansina.logging import configure_logging, get_logger


def main() -> None:
    try:
        settings = load_settings()
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

    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.server.host,
        port=settings.server.port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
