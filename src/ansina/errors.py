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


class FixtureError(HeartError):
    """A `heart.eval` tick fixture set failed validation. See `ansina.heart.eval`."""

    code: ClassVar[str] = "ansina.heart.fixture_invalid"


class BrainError(AnsinaError):
    """The remote Brain provider failed. See `ansina.brain`.

    Only ever raised at construction/selection time (mirroring `HeartError`'s own
    boot-time subclasses in `ansina.heart.runtime`) — a failure *during* a `stream()`
    call never raises; it surfaces as a terminal `BrainErrorEvent` instead (issue #12).
    """

    code: ClassVar[str] = "ansina.brain.error"


class AuthError(AnsinaError):
    """The RBAC identity/permission layer rejected an operation. See `ansina.auth`."""

    code: ClassVar[str] = "ansina.auth.error"


class TelemetryError(AnsinaError):
    """A telemetry/object-store operation failed. See `ansina.heart.eval.storage`
    (issue #59) — shared here, not kept local to that module, since issue #61's
    telemetry producer and issue #62's Vector-sidecar supervision are expected to
    raise against the same `[telemetry]` config surface.
    """

    code: ClassVar[str] = "ansina.telemetry.error"


class OpencodeError(AnsinaError):
    """The OpenCode-hosted executor ("Hands") failed. See `ansina.opencode`
    (issue #74's foundation, issue #75's `AgentRunner`/`OpencodeCliRunner`).
    """

    code: ClassVar[str] = "ansina.opencode.error"


class OpencodeUnavailableError(OpencodeError):
    """`OpencodeCliRunner.run()` was called on a runner with no resolved
    `opencode_path` — in practice unreachable once `build_opencode_service`
    (`ansina.opencode.preflight`) is the only real constructor, since it never
    hands out a runner on a failed preflight; kept as a defensive, never-
    silently-trust-the-caller guard rather than an assumption, the same
    `ansina.opencode.preflight` posture already applies to its own checks.
    """

    code: ClassVar[str] = "ansina.opencode.unavailable"


class OpencodeBusyError(OpencodeError):
    """`[opencode] queue_max_depth` would be exceeded by admitting this call.
    Raised synchronously at the top of `OpencodeCliRunner.run()`, before any
    subprocess is spawned — the one case issue #75's own acceptance criteria
    describe as refused "at the port," the explicit exception to `run()`'s
    otherwise-never-raises-after-invocation rule. Mapped to a problem response
    by a follow-up issue, not here (no route exists yet to map it from).
    """

    code: ClassVar[str] = "ansina.opencode.busy"


class OpencodeRunFailedError(OpencodeError):
    """A run request was rejected before any subprocess was spawned — today,
    only a `request.prompt` longer than `[opencode] max_prompt_chars`. Raised
    synchronously at the top of `OpencodeCliRunner.run()`, the same
    admission-control phase `OpencodeBusyError` occupies; once a subprocess is
    actually spawned (or its spawn is attempted), every failure resolves into
    a terminal `RunOutcome` instead (`ansina.opencode.events`), never a raise.
    """

    code: ClassVar[str] = "ansina.opencode.run_failed"
