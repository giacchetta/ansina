"""Wires `Settings.logging` into a `logging.config.dictConfig` call.

`configure_logging` is the single call site the rest of the app uses to turn on
logging — same "load via the typed accessor, never reach for the primitive yourself"
posture as `config.load_settings()` vs. bare `os.getenv`.
"""

from __future__ import annotations

import logging
import logging.config
from typing import TYPE_CHECKING, Any

from ansina.logging.redaction import register_secret

if TYPE_CHECKING:
    from ansina.config import Settings

_HANDLER_NAME = "ansina.json"
# Issue #61: the optional log-mirror handler's name — only ever added to `root.
# handlers` when `settings.telemetry.enabled`.
_TELEMETRY_HANDLER_NAME = "ansina.telemetry.log_mirror"


def configure_logging(settings: Settings) -> None:
    """Install a JSON `StreamHandler` on the root logger at `settings.logging.level`.

    Registers `settings.security.api_token` for redaction *before* installing the
    handler, so no log call can reach a sink before the real configured secret is
    masked. Safe to call more than once — `dictConfig` replaces the root logger's
    handlers wholesale each time rather than accumulating duplicates.

    Issue #61: when `settings.telemetry.enabled`, a second handler
    (`ansina.telemetry.log_mirror.TelemetryLogHandler`) is added, mirroring every
    record to a rotated file under `settings.telemetry.spool_dir` — through the
    *same* `"json"` formatter entry as the primary handler (`dictConfig`
    instantiates one formatter instance per name and shares it across every
    handler naming it), so the mirror's redaction can never diverge from the
    primary stream's. `settings.telemetry.enabled = false` (the default) adds
    nothing at all — the same "no-op when off" shape `[heart]`/`[security.oidc]`
    already use.
    """
    if settings.security.api_token is not None:
        register_secret(settings.security.api_token.get_secret_value())
    if settings.brain.api_key is not None:
        register_secret(settings.brain.api_key.get_secret_value())

    handlers: dict[str, Any] = {
        _HANDLER_NAME: {
            "class": "logging.StreamHandler",
            "formatter": "json",
            "stream": "ext://sys.stderr",
        },
    }
    root_handler_names = [_HANDLER_NAME]

    telemetry = settings.telemetry
    if telemetry.enabled:
        handlers[_TELEMETRY_HANDLER_NAME] = {
            "()": "ansina.telemetry.log_mirror.TelemetryLogHandler",
            "formatter": "json",
            "spool_dir": str(telemetry.spool_dir),
            "max_file_bytes": telemetry.max_file_bytes,
            "max_spool_bytes": telemetry.max_spool_bytes,
            "retention_hours": telemetry.retention_hours,
        }
        root_handler_names.append(_TELEMETRY_HANDLER_NAME)

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "json": {"()": "ansina.logging.formatter.JsonFormatter"},
            },
            "handlers": handlers,
            "root": {
                "level": settings.logging.level,
                "handlers": root_handler_names,
            },
        }
    )


def get_logger(name: str) -> logging.Logger:
    """The one call site the rest of the codebase uses instead of `logging.getLogger`.

    Prefer `get_logger(__name__)` at each call site.
    """
    return logging.getLogger(name)
