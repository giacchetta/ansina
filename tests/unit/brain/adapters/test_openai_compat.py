from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

import httpx2
import openai
import pytest

from ansina.brain.adapters.openai_compat import (
    OpenAICompatibleBrainProvider,
    classify_openai_error,
)
from ansina.brain.events import (
    BrainDone,
    BrainErrorClass,
    BrainErrorEvent,
    BrainTextDelta,
)
from ansina.brain.provider import BrainMessage, BrainRequest

_REQUEST = BrainRequest(
    messages=[BrainMessage(role="user", content="a prompt")],
    model="test-model",
    max_output_tokens=64,
)


def _text_chunk(text: str | None) -> SimpleNamespace:
    delta = SimpleNamespace(content=text)
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=delta)])


def _usage_chunk(
    *, prompt_tokens: int, completion_tokens: int, total_tokens: int
) -> SimpleNamespace:
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )
    return SimpleNamespace(usage=usage, choices=[])


def _empty_chunk() -> SimpleNamespace:
    return SimpleNamespace(usage=None, choices=[])


class _FakeRawStream:
    """Stands in for `openai.AsyncStream[ChatCompletionChunk]` — async-iterable, with
    an async `close()` whose call is observable via `close_calls`.
    """

    def __init__(self, chunks: list[Any], *, close_calls: list[str]) -> None:
        self._chunks = list(chunks)
        self._close_calls = close_calls

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[Any]:
        for chunk in self._chunks:
            yield chunk

    async def close(self) -> None:
        self._close_calls.append("closed")


class _FakeCompletions:
    def __init__(self, chunks: list[Any], *, close_calls: list[str]) -> None:
        self._chunks = chunks
        self._close_calls = close_calls
        self.create_calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _FakeRawStream:
        self.create_calls.append(kwargs)
        return _FakeRawStream(self._chunks, close_calls=self._close_calls)


class _FakeClient:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.chat = SimpleNamespace(completions=completions)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _provider(
    chunks: list[Any], *, close_calls: list[str] | None = None, **kwargs: Any
) -> tuple[OpenAICompatibleBrainProvider, _FakeCompletions, list[str], _FakeClient]:
    calls = close_calls if close_calls is not None else []
    completions = _FakeCompletions(chunks, close_calls=calls)
    client = _FakeClient(completions)
    provider = OpenAICompatibleBrainProvider(
        base_url="https://example.test/v1",
        api_key="k",
        timeout_seconds=1.0,
        max_retries=0,
        retry_initial_backoff_seconds=1.0,
        retry_max_backoff_seconds=1.0,
        client_factory=lambda *_args: cast("openai.AsyncOpenAI", client),
        **kwargs,
    )
    return provider, completions, calls, client


async def test_text_deltas_and_authoritative_usage() -> None:
    provider, _completions, close_calls, _client = _provider(
        [
            _text_chunk("hel"),
            _text_chunk("lo"),
            _usage_chunk(prompt_tokens=3, completion_tokens=2, total_tokens=5),
        ]
    )

    events = [event async for event in provider.stream(_REQUEST)]

    assert events[0] == BrainTextDelta("hel")
    assert events[1] == BrainTextDelta("lo")
    assert isinstance(events[2], BrainDone)
    assert events[2].usage is not None
    assert events[2].usage.prompt_tokens == 3
    assert events[2].usage.completion_tokens == 2
    assert events[2].usage.authoritative is True
    assert close_calls == ["closed"]


async def test_stream_without_usage_chunk_is_estimated_and_nonauthoritative() -> None:
    provider, _completions, _close_calls, _client = _provider(
        [_text_chunk("abcd" * 3)]  # 12 chars
    )

    (delta, done) = [event async for event in provider.stream(_REQUEST)]

    assert delta == BrainTextDelta("abcd" * 3)
    assert isinstance(done, BrainDone)
    assert done.usage is not None
    assert done.usage.authoritative is False
    assert done.usage.completion_tokens == 3  # 12 chars // 4
    assert done.usage.prompt_tokens == len("a prompt") // 4


async def test_chunk_with_no_choices_is_skipped() -> None:
    provider, _completions, _close_calls, _client = _provider(
        [_empty_chunk(), _text_chunk("hi")]
    )

    events = [event async for event in provider.stream(_REQUEST)]

    assert len(events) == 2
    assert events[0] == BrainTextDelta("hi")
    assert isinstance(events[1], BrainDone)


async def test_chunk_with_no_delta_content_is_skipped() -> None:
    provider, _completions, _close_calls, _client = _provider(
        [_text_chunk(None), _text_chunk("hi")]
    )

    events = [event async for event in provider.stream(_REQUEST)]

    assert events[0] == BrainTextDelta("hi")


async def test_create_receives_model_and_max_tokens() -> None:
    provider, completions, _close_calls, _client = _provider([])

    [_event async for _event in provider.stream(_REQUEST)]

    (call,) = completions.create_calls
    assert call["model"] == "test-model"
    assert call["max_tokens"] == 64
    assert call["stream"] is True
    assert call["stream_options"] == {"include_usage": True}


async def test_cancellation_closes_the_raw_stream_promptly() -> None:
    """Issue #12 acceptance criterion #5: caller aborts stops the in-flight request
    promptly — verified here by closing the outer generator after one event and
    asserting the underlying SDK stream's `close()` was awaited immediately, not left
    to garbage collection.
    """
    provider, _completions, close_calls, _client = _provider(
        [_text_chunk("a"), _text_chunk("b"), _text_chunk("c")]
    )

    agen = provider.stream(_REQUEST)
    first = await agen.__anext__()
    assert first == BrainTextDelta("a")
    assert close_calls == []

    await agen.aclose()

    assert close_calls == ["closed"]


async def test_partial_usage_estimated_after_mid_stream_failure() -> None:
    close_calls: list[str] = []
    completions = _FakeCompletions([], close_calls=close_calls)

    async def _raising_create(**kwargs: Any) -> _FakeRawStream:
        raise openai.APIConnectionError(request=_request())

    completions.create = _raising_create  # type: ignore[method-assign]
    client = _FakeClient(completions)
    provider = OpenAICompatibleBrainProvider(
        base_url="https://example.test/v1",
        api_key="k",
        timeout_seconds=1.0,
        max_retries=0,
        retry_initial_backoff_seconds=1.0,
        retry_max_backoff_seconds=1.0,
        client_factory=lambda *_args: cast("openai.AsyncOpenAI", client),
    )

    (event,) = [event async for event in provider.stream(_REQUEST)]

    assert isinstance(event, BrainErrorEvent)
    assert event.error_class is BrainErrorClass.TRANSPORT
    assert event.usage is not None
    assert event.usage.authoritative is False
    assert event.usage.prompt_tokens == len("a prompt") // 4
    assert event.usage.completion_tokens == 0


async def test_aclose_closes_the_client() -> None:
    provider, _completions, _close_calls, client = _provider([])

    await provider.aclose()

    assert client.closed is True


def test_cost_computed_when_prices_configured() -> None:
    provider, _completions, _close_calls, _client = _provider(
        [], price_per_1m_input_tokens=1.0, price_per_1m_output_tokens=2.0
    )

    cost = provider._cost_usd(1_000_000, 500_000)

    assert cost == pytest.approx(1.0 + 1.0)


def test_cost_is_none_when_prices_not_configured() -> None:
    provider, _completions, _close_calls, _client = _provider([])

    assert provider._cost_usd(1_000, 1_000) is None


def test_partial_usage_is_none_before_any_call() -> None:
    provider, _completions, _close_calls, _client = _provider([])

    assert provider._partial_usage() is None


def test_default_client_factory_falls_back_to_placeholder_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class _RecordingAsyncOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(
        "ansina.brain.adapters.openai_compat.openai.AsyncOpenAI",
        _RecordingAsyncOpenAI,
    )
    from ansina.brain.adapters.openai_compat import _default_client_factory

    _default_client_factory("https://example.test/v1", None, 30.0)

    assert calls[0]["api_key"] == "not-needed"
    assert calls[0]["max_retries"] == 0

    _default_client_factory("https://example.test/v1", "real-key", 30.0)

    assert calls[1]["api_key"] == "real-key"


def _request() -> httpx2.Request:
    return httpx2.Request("POST", "https://example.test/v1/chat/completions")


def _response(status: int) -> httpx2.Response:
    return httpx2.Response(status, request=_request())


def test_classify_timeout() -> None:
    assert classify_openai_error(openai.APITimeoutError(request=_request())) is (
        BrainErrorClass.TIMEOUT
    )


def test_classify_rate_limit() -> None:
    exc = openai.RateLimitError("rate limited", response=_response(429), body=None)
    assert classify_openai_error(exc) is BrainErrorClass.RATE_LIMIT


def test_classify_transport() -> None:
    exc = openai.APIConnectionError(request=_request())
    assert classify_openai_error(exc) is BrainErrorClass.TRANSPORT


def test_classify_provider_client_on_4xx() -> None:
    exc = openai.APIStatusError("bad request", response=_response(400), body=None)
    assert classify_openai_error(exc) is BrainErrorClass.PROVIDER_CLIENT


def test_classify_provider_server_on_5xx() -> None:
    exc = openai.APIStatusError("server error", response=_response(500), body=None)
    assert classify_openai_error(exc) is BrainErrorClass.PROVIDER_SERVER


def test_classify_internal_fallback() -> None:
    assert classify_openai_error(RuntimeError("mystery")) is BrainErrorClass.INTERNAL
