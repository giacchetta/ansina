from __future__ import annotations

import pytest

from ansina.errors import HeartError
from ansina.heart.runtime import (
    BaseHeartRuntime,
    HeartContextOverflowError,
    HeartLoadError,
    HeartNotLoadedError,
    HeartRuntime,
    HeartUnavailableError,
)


class _FakeHeartRuntime(BaseHeartRuntime):
    """A minimal concrete `BaseHeartRuntime`: 1 token per character, echoes back
    what it was asked to generate — exercises the shared guard/budget logic without
    a real backend.
    """

    def __init__(
        self, *, context_tokens: int = 100, max_output_tokens: int = 10
    ) -> None:
        super().__init__(
            context_tokens=context_tokens, max_output_tokens=max_output_tokens
        )
        self.load_calls = 0
        self.unload_calls = 0

    def _load_backend(self) -> None:
        self.load_calls += 1

    def _generate(self, prompt: str, max_tokens: int) -> str:
        return f"generated:{prompt}:{max_tokens}"

    def _token_count(self, text: str) -> int:
        return len(text)

    def _unload_backend(self) -> None:
        self.unload_calls += 1


def test_conforms_to_heart_runtime_protocol() -> None:
    assert isinstance(_FakeHeartRuntime(), HeartRuntime)


def test_context_tokens_property() -> None:
    assert _FakeHeartRuntime(context_tokens=42).context_tokens == 42


def test_generate_before_load_raises_not_loaded() -> None:
    runtime = _FakeHeartRuntime()

    with pytest.raises(HeartNotLoadedError):
        runtime.generate("hi")


def test_load_is_idempotent() -> None:
    runtime = _FakeHeartRuntime()

    runtime.load()
    runtime.load()

    assert runtime.load_calls == 1


def test_generate_after_load_returns_backend_output() -> None:
    runtime = _FakeHeartRuntime()
    runtime.load()

    assert runtime.generate("hi", max_tokens=5) == "generated:hi:5"


def test_generate_falls_back_to_configured_max_output_tokens() -> None:
    runtime = _FakeHeartRuntime(max_output_tokens=7)
    runtime.load()

    assert runtime.generate("hi") == "generated:hi:7"


def test_generate_over_budget_raises_context_overflow() -> None:
    runtime = _FakeHeartRuntime(context_tokens=10, max_output_tokens=5)
    runtime.load()

    with pytest.raises(HeartContextOverflowError, match="10-token budget"):
        runtime.generate("x" * 10, max_tokens=5)


def test_generate_exactly_at_budget_succeeds() -> None:
    runtime = _FakeHeartRuntime(context_tokens=10, max_output_tokens=5)
    runtime.load()

    assert runtime.generate("x" * 5, max_tokens=5) == f"generated:{'x' * 5}:5"


def test_token_count_delegates_to_backend() -> None:
    assert _FakeHeartRuntime().token_count("hello") == 5


def test_unload_is_idempotent_when_never_loaded() -> None:
    runtime = _FakeHeartRuntime()

    runtime.unload()

    assert runtime.unload_calls == 0


def test_unload_after_load_calls_backend() -> None:
    runtime = _FakeHeartRuntime()
    runtime.load()

    runtime.unload()

    assert runtime.unload_calls == 1


def test_is_healthy_false_before_load() -> None:
    assert _FakeHeartRuntime().is_healthy() is False


def test_is_healthy_true_after_load() -> None:
    runtime = _FakeHeartRuntime()
    runtime.load()

    assert runtime.is_healthy() is True


def test_is_healthy_false_when_token_count_raises() -> None:
    class _Failing(_FakeHeartRuntime):
        def _token_count(self, text: str) -> int:
            raise HeartError("boom")

    runtime = _Failing()
    runtime.load()

    assert runtime.is_healthy() is False


@pytest.mark.parametrize(
    ("error_cls", "code"),
    [
        (HeartUnavailableError, "ansina.heart.unavailable"),
        (HeartLoadError, "ansina.heart.load_failed"),
        (HeartNotLoadedError, "ansina.heart.not_loaded"),
        (HeartContextOverflowError, "ansina.heart.context_overflow"),
    ],
)
def test_heart_error_subclasses_have_stable_codes(
    error_cls: type[HeartError], code: str
) -> None:
    assert issubclass(error_cls, HeartError)
    assert error_cls.code == code
