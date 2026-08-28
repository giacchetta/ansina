"""OpenAI-compatible adapter for `BrainProvider`. See issue #12.

Works against any endpoint speaking the OpenAI chat-completions protocol — `base_url`,
`api_key`, and `model` are all configured (`BrainSettings`), never hardcoded to one
vendor. `openai` is a *base* dependency (see `pyproject.toml`), unlike the optional
`ansina[mlx]` extra, so — unlike `heart/adapters/mlx.py` — this module imports the SDK
at module scope.

The SDK's own `max_retries` is fixed to `0` at client construction (`_default_client_
factory`): retry is `BaseBrainProvider`'s job, so it can log every attempt uniformly and
respect the "only retry before any text has reached the caller" rule — a second,
SDK-owned retry loop underneath it would double-retry invisibly.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from typing import cast

import openai
from openai.types.chat import ChatCompletionMessageParam

from ansina.brain.events import (
    BrainDone,
    BrainErrorClass,
    BrainEvent,
    BrainTextDelta,
    BrainUsage,
)
from ansina.brain.provider import BaseBrainProvider, BrainRequest
from ansina.logging import get_logger

logger = get_logger(__name__)

# Rough, provider-agnostic fallback used only to build a *non-authoritative*
# `BrainUsage` when a stream ends (successfully or not) before the provider's own
# usage totals arrive. Never used when real numbers are available.
_ESTIMATED_CHARS_PER_TOKEN = 4

ClientFactory = Callable[[str, str | None, float], "openai.AsyncOpenAI"]


def _default_client_factory(
    base_url: str, api_key: str | None, timeout_seconds: float
) -> openai.AsyncOpenAI:
    # A keyless custom endpoint (a local OpenAI-compatible server) is legitimate
    # (`ansina.brain.selection` is what refuses a keyless *default* host) — the SDK
    # itself requires *a* string, so a placeholder stands in rather than falling back
    # to a stray `OPENAI_API_KEY` env var Ansina never configured.
    return openai.AsyncOpenAI(
        base_url=base_url,
        api_key=api_key if api_key is not None else "not-needed",
        timeout=timeout_seconds,
        max_retries=0,
    )


def classify_openai_error(exc: Exception) -> BrainErrorClass:
    """`openai`'s own exception hierarchy -> the provider-agnostic `BrainErrorClass`
    every caller branches on. Order matters: `APITimeoutError` subclasses
    `APIConnectionError`, and `RateLimitError` subclasses `APIStatusError`, so the more
    specific checks must run first.
    """
    if isinstance(exc, openai.APITimeoutError):
        return BrainErrorClass.TIMEOUT
    if isinstance(exc, openai.RateLimitError):
        return BrainErrorClass.RATE_LIMIT
    if isinstance(exc, openai.APIConnectionError):
        return BrainErrorClass.TRANSPORT
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code >= 500:
            return BrainErrorClass.PROVIDER_SERVER
        return BrainErrorClass.PROVIDER_CLIENT
    return BrainErrorClass.INTERNAL


class OpenAICompatibleBrainProvider(BaseBrainProvider):
    """`BrainProvider` backed by any OpenAI-compatible chat-completions endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        timeout_seconds: float,
        max_retries: int,
        retry_initial_backoff_seconds: float,
        retry_max_backoff_seconds: float,
        price_per_1m_input_tokens: float | None = None,
        price_per_1m_output_tokens: float | None = None,
        client_factory: ClientFactory = _default_client_factory,
    ) -> None:
        super().__init__(
            max_retries=max_retries,
            retry_initial_backoff_seconds=retry_initial_backoff_seconds,
            retry_max_backoff_seconds=retry_max_backoff_seconds,
        )
        self._price_in = price_per_1m_input_tokens
        self._price_out = price_per_1m_output_tokens
        self._client = client_factory(base_url, api_key, timeout_seconds)
        # Reset at the start of every `_stream()` call; read by `_partial_usage()` if
        # that call later raises. Chars, not tokens — see `_ESTIMATED_CHARS_PER_TOKEN`.
        self._prompt_chars = 0
        self._completion_chars = 0

    async def _stream(self, request: BrainRequest) -> AsyncGenerator[BrainEvent]:
        self._prompt_chars = sum(len(m.content) for m in request.messages)
        self._completion_chars = 0
        # `BrainMessage.role` is a `Literal` value known only at runtime, so mypy can't
        # narrow which of `ChatCompletionMessageParam`'s per-role TypedDict variants
        # each dict literal below satisfies — the shape is correct by construction
        # (role + content is valid for all three roles Ansina uses), hence the cast.
        messages = cast(
            "list[ChatCompletionMessageParam]",
            [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
        )
        raw_stream = await self._client.chat.completions.create(
            model=request.model,
            messages=messages,
            max_tokens=request.max_output_tokens,
            stream=True,
            stream_options={"include_usage": True},
        )
        try:
            async for chunk in raw_stream:
                if chunk.usage is not None:
                    yield BrainDone(
                        usage=self._usage_from_totals(
                            prompt_tokens=chunk.usage.prompt_tokens,
                            completion_tokens=chunk.usage.completion_tokens,
                            total_tokens=chunk.usage.total_tokens,
                            authoritative=True,
                        )
                    )
                    return
                if not chunk.choices:
                    continue
                text = chunk.choices[0].delta.content
                if text:
                    self._completion_chars += len(text)
                    yield BrainTextDelta(text=text)
            # Some OpenAI-compatible servers don't honor `stream_options` and never
            # send a usage-bearing final chunk — the stream still ended cleanly, so
            # this is still a `BrainDone`, just with an estimated (non-authoritative)
            # usage rather than no usage at all.
            yield BrainDone(usage=self._estimated_usage())
        finally:
            await raw_stream.close()

    def _classify_error(self, exc: Exception) -> BrainErrorClass:
        return classify_openai_error(exc)

    def _partial_usage(self) -> BrainUsage | None:
        if self._completion_chars == 0 and self._prompt_chars == 0:
            return None
        return self._estimated_usage()

    async def _aclose(self) -> None:
        await self._client.close()

    def _estimated_usage(self) -> BrainUsage:
        prompt_tokens = self._prompt_chars // _ESTIMATED_CHARS_PER_TOKEN
        completion_tokens = self._completion_chars // _ESTIMATED_CHARS_PER_TOKEN
        return self._usage_from_totals(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            authoritative=False,
        )

    def _usage_from_totals(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        authoritative: bool,
    ) -> BrainUsage:
        return BrainUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            authoritative=authoritative,
            cost_usd=self._cost_usd(prompt_tokens, completion_tokens),
        )

    def _cost_usd(self, prompt_tokens: int, completion_tokens: int) -> float | None:
        if self._price_in is None or self._price_out is None:
            return None
        return (
            prompt_tokens / 1_000_000 * self._price_in
            + completion_tokens / 1_000_000 * self._price_out
        )
