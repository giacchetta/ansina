"""Stable, machine-readable error taxonomy for Ansina.

Every exception the application raises deliberately (as opposed to letting a
third-party exception bubble up) should subclass :class:`AnsinaError`. The API layer
maps `code` to an HTTP response; log lines and clients can key off `code` without
parsing message text.

Start minimal (see issue #3): add a subclass per failure category as one is actually
needed, rather than pre-inventing a taxonomy no code exercises yet.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar


class AnsinaError(Exception):
    """Base for every deliberately-raised Ansina exception.

    Subclasses must declare their own `code` — the base's `code` exists only so this
    class itself is instantiable, not as a fallback subclasses are meant to inherit
    silently.
    """

    code: ClassVar[str] = "ansina.error"

    def __init__(
        self, message: str, *, details: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self._details: Mapping[str, Any] = dict(details) if details else {}

    def __init_subclass__(cls, **kwargs: Any) -> None:  # noqa: ANN401
        super().__init_subclass__(**kwargs)
        if "code" not in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} must declare its own `code` — it may not inherit "
                f"{cls.__mro__[1].__name__}'s."
            )

    @property
    def details(self) -> Mapping[str, Any]:
        """Structured context, e.g. to merge into a log line or an error response."""
        return self._details


class ConfigurationError(AnsinaError):
    """Configuration failed to load or validate. See `ansina.config.ConfigError`."""

    code: ClassVar[str] = "ansina.config.invalid"


class StorageError(AnsinaError):
    """The SQLite persistence layer failed. See `ansina.storage`."""

    code: ClassVar[str] = "ansina.storage.error"


class HeartError(AnsinaError):
    """The in-process Heart runtime failed. See `ansina.heart`."""

    code: ClassVar[str] = "ansina.heart.error"


class BrainError(AnsinaError):
    """The remote Brain provider failed. See `ansina.brain`.

    Only ever raised at construction/selection time (mirroring `HeartError`'s own
    boot-time subclasses in `ansina.heart.runtime`) — a failure *during* a `stream()`
    call never raises; it surfaces as a terminal `BrainErrorEvent` instead (issue #12).
    """

    code: ClassVar[str] = "ansina.brain.error"
