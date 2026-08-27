from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncGenerator, Callable

import pytest

from ansina.brain.events import (
    BrainDone,
    BrainErrorClass,
    BrainErrorEvent,
    BrainEvent,
    BrainTextDelta,
    BrainUsage,
)
from ansina.brain.provider import BaseBrainProvider, BrainMessage, BrainRequest

_REQUEST = BrainRequest(
    messages=[BrainMessage(role="user", content="hi")],
    model="test-model",
    max_output_tokens=64,
)


class _FakeBrainProvider(BaseBrainProvider):
    """Each entry in `attempts` is one `_stream()` call's worth of items, yielded in
    order; a `BaseException` item is raised instead of yielded, mid-generator — lets a
    test express "yields text, then fails" for the same attempt.
    """

    def __init__(
        self,
        attempts: list[list[BrainEvent | BaseException]],
        *,
        classify: Callable[[Exception], BrainErrorClass] = (
            lambda exc: BrainErrorClass.PROVIDER_CLIENT
        ),
        partial_usage: BrainUsage | None = None,
        max_retries: int = 0,
        retry_initial_backoff_seconds: float = 1.0,
        retry_max_backoff_seconds: float = 30.0,
    ) -> None:
        self.sleep_calls: list[float] = []
        self.call_count = 0
        self.closed = False
        self._attempts = list(attempts)
        self._classify = classify
        self._partial = partial_usage
        super().__init__(
            max_retries=max_retries,
            retry_initial_backoff_seconds=retry_initial_backoff_seconds,
            retry_max_backoff_seconds=retry_max_backoff_seconds,
            sleep=self._record_sleep,
        )

    async def _record_sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)

    async def _stream(self, request: BrainRequest) -> AsyncGenerator[BrainEvent]:
        self.call_count += 1
        for item in self._attempts.pop(0):
            if isinstance(item, BaseException):
                raise item
            yield item

    def _classify_error(self, exc: Exception) -> BrainErrorClass:
        return self._classify(exc)

    def _partial_usage(self) -> BrainUsage | None:
        return self._partial

    async def _aclose(self) -> None:
        self.closed = True


async def _collect(
    provider: BaseBrainProvider, request: BrainRequest
) -> list[BrainEvent]:
    return [event async for event in provider.stream(request)]


async def test_stream_returns_synchronously() -> None:
    """The load-bearing OpenClaw invariant (blueprint §1): `stream()` is a plain
    method, not `async def` — calling it must not itself be awaitable.
    """
    provider = _FakeBrainProvider([[BrainDone()]])

    assert inspect.iscoroutinefunction(provider.stream) is False
    result = provider.stream(_REQUEST)
    assert hasattr(result, "__anext__")
    await result.aclose()


async def test_happy_path_terminates_with_done() -> None:
    usage = BrainUsage(
        prompt_tokens=1, completion_tokens=2, total_tokens=3, authoritative=True
    )
    provider = _FakeBrainProvider(
        [[BrainTextDelta("a"), BrainTextDelta("b"), BrainDone(usage=usage)]]
    )

    events = await _collect(provider, _REQUEST)

    assert events == [BrainTextDelta("a"), BrainTextDelta("b"), BrainDone(usage=usage)]
    assert provider.call_count == 1


async def test_mid_stream_failure_yields_terminal_error_event() -> None:
    """Issue #12 acceptance criterion #2: a simulated mid-stream failure surfaces as a
    terminal error result, never as an unhandled exception.
    """
    provider = _FakeBrainProvider(
        [[RuntimeError("boom")]],
        classify=lambda exc: BrainErrorClass.PROVIDER_CLIENT,
    )

    events = await _collect(provider, _REQUEST)

    assert len(events) == 1
    (event,) = events
    assert isinstance(event, BrainErrorEvent)
    assert event.error_class is BrainErrorClass.PROVIDER_CLIENT
    assert event.retryable is False
    assert event.message == "boom"


async def test_non_retryable_class_is_not_retried() -> None:
    provider = _FakeBrainProvider(
        [[RuntimeError("boom")]],
        classify=lambda exc: BrainErrorClass.PROVIDER_CLIENT,
        max_retries=3,
    )

    await _collect(provider, _REQUEST)

    assert provider.call_count == 1
    assert provider.sleep_calls == []


async def test_retryable_class_retries_up_to_max_and_no_further() -> None:
    """Issue #12 acceptance criterion #3: bounded retry, observable in logs (here,
    observed via the injected `sleep` hook standing in for the backoff delay).
    """
    provider = _FakeBrainProvider(
        [
            [RuntimeError("1")],
            [RuntimeError("2")],
            [RuntimeError("3")],
        ],
        classify=lambda exc: BrainErrorClass.TIMEOUT,
        max_retries=2,
        retry_initial_backoff_seconds=1.0,
        retry_max_backoff_seconds=30.0,
    )

    events = await _collect(provider, _REQUEST)

    assert provider.call_count == 3
    assert provider.sleep_calls == [1.0, 2.0]
    (event,) = events
    assert isinstance(event, BrainErrorEvent)
    assert event.retryable is True


async def test_failure_after_text_delta_is_not_retried() -> None:
    """A retryable-classified failure that happens after text has already reached the
    caller must not be retried — retrying would duplicate or reorder output the caller
    already has, regardless of remaining retry budget.
    """
    provider = _FakeBrainProvider(
        [[BrainTextDelta("partial"), RuntimeError("boom")]],
        classify=lambda exc: BrainErrorClass.TIMEOUT,
        max_retries=3,
    )

    events = await _collect(provider, _REQUEST)

    assert events[0] == BrainTextDelta("partial")
    assert isinstance(events[1], BrainErrorEvent)
    assert provider.call_count == 1
    assert provider.sleep_calls == []


async def test_cancellation_propagates_rather_than_becoming_an_error_event() -> None:
    provider = _FakeBrainProvider([[asyncio.CancelledError()]])

    with pytest.raises(asyncio.CancelledError):
        await _collect(provider, _REQUEST)


async def test_error_event_carries_partial_usage() -> None:
    usage = BrainUsage(
        prompt_tokens=5, completion_tokens=1, total_tokens=6, authoritative=False
    )
    provider = _FakeBrainProvider(
        [[RuntimeError("boom")]],
        classify=lambda exc: BrainErrorClass.PROVIDER_CLIENT,
        partial_usage=usage,
    )

    (event,) = await _collect(provider, _REQUEST)

    assert isinstance(event, BrainErrorEvent)
    assert event.usage == usage


async def test_aclose_calls_hook() -> None:
    provider = _FakeBrainProvider([[BrainDone()]])

    await provider.aclose()

    assert provider.closed is True


def test_negative_max_retries_rejected() -> None:
    with pytest.raises(ValueError, match="max_retries must be >= 0"):
        _FakeBrainProvider([[BrainDone()]], max_retries=-1)
