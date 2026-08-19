"""Wires `Settings.logging` into a `logging.config.dictConfig` call.

`configure_logging` is the single call site the rest of the app uses to turn on
logging — same "load via the typed accessor, never reach for the primitive yourself"
posture as `config.load_settings()` vs. bare `os.getenv`.
"""

from __future__ import annotations

import logging
import logging.config
from typing import TYPE_CHECKING

from ansina.logging.redaction import register_secret

if TYPE_CHECKING:
    from ansina.config import Settings

_HANDLER_NAME = "ansina.json"


def configure_logging(settings: Settings) -> None:
    """Install a JSON `StreamHandler` on the root logger at `settings.logging.level`.

    Registers `settings.security.api_token` for redaction *before* installing the
    handler, so no log call can reach a sink before the real configured secret is
    masked. Safe to call more than once — `dictConfig` replaces the root logger's
    handlers wholesale each time rather than accumulating duplicates.
    """
    if settings.security.api_token is not None:
        register_secret(settings.security.api_token.get_secret_value())

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "json": {"()": "ansina.logging.formatter.JsonFormatter"},
            },
            "handlers": {
                _HANDLER_NAME: {
                    "class": "logging.StreamHandler",
                    "formatter": "json",
                    "stream": "ext://sys.stderr",
                },
            },
            "root": {
                "level": settings.logging.level,
                "handlers": [_HANDLER_NAME],
            },
        }
    )


def get_logger(name: str) -> logging.Logger:
    """The one call site the rest of the codebase uses instead of `logging.getLogger`.

    Prefer `get_logger(__name__)` at each call site.
    """
    return logging.getLogger(name)
