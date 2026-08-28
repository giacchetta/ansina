"""`BrainProvider` — the port every remote Brain adapter implements. See issue #12.

The Brain is a 35B+ reasoning model reached over the network (`docs/architecture/
blueprint.md` §3) — unlike `HeartRuntime` (`ansina.heart.runtime`), there is no
in-process backend to guard, but the same shape applies: `BrainProvider` is a
structural `Protocol` so an adapter needs no import-time coupling to this module, and
`BaseBrainProvider` owns the invariants every adapter must share rather than
re-implementing them.

The load-bearing invariant, taken from OpenClaw's `ApiProvider` (blueprint §1): **a
stream must never throw after invocation**. `stream()` returns synchronously — it is a
plain method returning an async iterator, not `async def` — and every failure, at any
point after the first event would have been requested, arrives as a terminal
`BrainErrorEvent`, never as a raised exception into the caller's `async for`. A
`BrainError` (`ansina.errors`) is only ever raised at construction/selection time
(`ansina.brain.selection`), the same boot-time-only shape `HeartUnavailableError` has.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from ansina.brain.events import (
    RETRYABLE_ERROR_CLASSES,
    BrainDone,
    BrainErrorClass,
    BrainErrorEvent,
    BrainEvent,
    BrainTextDelta,
    BrainUsage,
)
from ansina.brain.retry import backoff_seconds
from ansina.logging import get_logger

logger = get_logger(__name__)

BrainRole = Literal["system", "user", "assistant"]


@dataclass(frozen=True, slots=True)
class BrainMessage:
    """One chat-shaped message in a `BrainRequest`. Ansina has no multi-modal or
    tool-call surface yet, so this is deliberately just role + text.
    """

    role: BrainRole
    content: str


@dataclass(frozen=True, slots=True)
class BrainRequest:
    """Everything a `stream()` call needs. `model` is per-request (not adapter-fixed)
    so a future caller can route to a different configured model without a new
    provider instance; `ansina.brain.selection.build_brain_provider` fills it from
    `BrainSettings.model` by default.
    """

    messages: Sequence[BrainMessage]
    model: str
    max_output_tokens: int


@runtime_checkable
class BrainProvider(Protocol):
    """The port. Structural, like `HeartRuntime` — see module docstring."""

    def stream(self, request: BrainRequest) -> AsyncGenerator[BrainEvent]:
        """Never raises. See module docstring for the never-throw-after-invocation
        contract; a call that fails before any event is produced (e.g. a bad request)
        still surfaces as the stream's first and only event, not an exception.

        Returns an async *generator*, not just an async iterator — `aclose()` on the
        returned value is how a caller cancels an in-flight call (issue #12's
        "caller aborts stops the in-flight request promptly" criterion).
        """
        ...

    async def aclose(self) -> None:
        """Release any held client/connection resources. Safe to call more than once
        and safe to call even if `stream()` was never called.
        """
        ...


Sleep = Callable[[float], Awaitable[None]]


class BaseBrainProvider(ABC):
    """Shared retry and never-throw logic for every `BrainProvider` adapter.

    Subclasses implement three `_*` hooks — `_stream`, `_classify_error`,
    `_partial_usage` — plus `_aclose`; this class owns:

    - **Never-throw**: any exception `_stream()` raises is caught, classified, and
      turned into a terminal `BrainErrorEvent` rather than propagating — except
      `asyncio.CancelledError`, which is re-raised untouched. Cancellation is the
      caller's own signal (issue #12's "caller aborts stops the in-flight request
      promptly" criterion), not a provider failure, and swallowing it would break
      structured concurrency for whatever `asyncio.Task` this generator is running in.
    - **Bounded retry**: a `retryable`-classified failure is retried with exponential
      backoff (`ansina.brain.retry.backoff_seconds`), up to `max_retries` times, and
      only while **no `BrainTextDelta` has reached the caller yet** — once partial text
      has been yielded, a transparent retry would duplicate or reorder output the
      caller already has, so any failure past that point is terminal immediately, retry
      budget or not. Every retry is logged at warning level with the attempt number,
      error class, and backoff delay actually used.
    """

    def __init__(
        self,
        *,
        max_retries: int,
        retry_initial_backoff_seconds: float,
        retry_max_backoff_seconds: float,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self._max_retries = max_retries
        self._retry_initial = retry_initial_backoff_seconds
        self._retry_max = retry_max_backoff_seconds
        self._sleep = sleep

    def stream(self, request: BrainRequest) -> AsyncGenerator[BrainEvent]:
        return self._run(request)

    async def _run(self, request: BrainRequest) -> AsyncGenerator[BrainEvent]:
        attempt = 0
        yielded_text = False
        while True:
            attempt += 1
            # `gen` is closed explicitly in `finally` rather than left to GC: if the
            # caller cancels/closes this generator (`GeneratorExit`, not caught below
            # since it isn't an `Exception`) while suspended at `yield event`, the
            # `finally` still runs and closes `gen` immediately — that's what makes
            # the adapter's own `finally: await raw_stream.close()` fire promptly
            # instead of waiting for the inner generator to be garbage-collected
            # (issue #12's "caller aborts stops the in-flight request promptly").
            gen = self._stream(request)
            try:
                async for event in gen:
                    if isinstance(event, BrainTextDelta):
                        yielded_text = True
                    yield event
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error_class = self._classify_error(exc)
                retryable = error_class in RETRYABLE_ERROR_CLASSES
                if retryable and not yielded_text and attempt <= self._max_retries:
                    delay = backoff_seconds(
                        attempt,
                        initial=self._retry_initial,
                        maximum=self._retry_max,
                    )
                    logger.warning(
                        "brain call failed, retrying",
                        extra={
                            "attempt": attempt,
                            "error_class": error_class.value,
                            "delay_seconds": delay,
                        },
                    )
                    await self._sleep(delay)
                    continue
                logger.warning(
                    "brain call failed terminally",
                    extra={
                        "attempt": attempt,
                        "error_class": error_class.value,
                        "retryable": retryable,
                        "yielded_text": yielded_text,
                    },
                )
                yield BrainErrorEvent(
                    error_class=error_class,
                    message=str(exc),
                    retryable=retryable,
                    usage=self._partial_usage(),
                )
                return
            finally:
                # Safe to call unconditionally: `aclose()` on an already-exhausted or
                # already-raised generator is a no-op, so this never double-runs the
                # adapter's own cleanup.
                await gen.aclose()

    async def aclose(self) -> None:
        await self._aclose()

    @abstractmethod
    def _stream(self, request: BrainRequest) -> AsyncGenerator[BrainEvent]:
        """Yields `BrainTextDelta`s and, on success, a final `BrainDone`. Free to
        raise any exception on failure — `_run` above is what turns that into a
        terminal `BrainErrorEvent`; this hook must never construct one itself.
        """
        ...

    @abstractmethod
    def _classify_error(self, exc: Exception) -> BrainErrorClass: ...

    @abstractmethod
    def _partial_usage(self) -> BrainUsage | None:
        """Best-effort accounting for whatever was generated before `_stream` raised.
        Always non-authoritative when non-`None` — see `BrainUsage.authoritative`.
        """
        ...

    @abstractmethod
    async def _aclose(self) -> None: ...


__all__ = [
    "BaseBrainProvider",
    "BrainDone",
    "BrainErrorClass",
    "BrainErrorEvent",
    "BrainEvent",
    "BrainMessage",
    "BrainProvider",
    "BrainRequest",
    "BrainRole",
    "BrainTextDelta",
    "BrainUsage",
]
