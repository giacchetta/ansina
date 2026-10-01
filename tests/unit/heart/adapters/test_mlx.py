from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ansina.errors import HeartError
from ansina.heart.adapters.mlx import MlxHeartRuntime, _default_loader
from ansina.heart.runtime import HeartLoadError


class _FakeTokenizer:
    def encode(self, text: str) -> list[int]:
        return list(range(len(text)))


class _FakeChatTokenizer(_FakeTokenizer):
    """A tokenizer that *does* expose a chat template accepting arbitrary extra
    kwargs (the real `transformers`/`mlx_lm` shape, forwarded into the Jinja render
    context) — unlike `_FakeTokenizer`, whose absence of `apply_chat_template` is
    itself what `test_generate_calls_mlx_lm_generate_and_returns_its_result` already
    covers (the getattr-`None` fallback branch).
    """

    def __init__(self) -> None:
        self.last_kwargs: dict[str, Any] = {}

    def apply_chat_template(
        self,
        messages: list[dict[str, str]],
        *,
        tokenize: bool,
        add_generation_prompt: bool,
        **kwargs: Any,
    ) -> str:
        assert tokenize is False
        assert add_generation_prompt is True
        assert messages == [{"role": "user", "content": messages[0]["content"]}]
        self.last_kwargs = kwargs
        return f"<chat>{messages[0]['content']}</chat>"


class _FakeLegacyChatTokenizer(_FakeTokenizer):
    """A tokenizer whose `apply_chat_template` doesn't forward arbitrary keyword
    arguments at all — the rarer shape `_render_prompt`'s `TypeError` fallback
    exists for.
    """

    def apply_chat_template(
        self,
        messages: list[dict[str, str]],
        *,
        tokenize: bool,
        add_generation_prompt: bool,
    ) -> str:
        return f"<chat>{messages[0]['content']}</chat>"


def _fake_loader(model_path: Path) -> tuple[object, object]:
    return object(), _FakeTokenizer()


@pytest.fixture
def runtime(tmp_path: Path) -> MlxHeartRuntime:
    return MlxHeartRuntime(
        tmp_path, context_tokens=100, max_output_tokens=10, loader=_fake_loader
    )


def test_load_calls_injected_loader(runtime: MlxHeartRuntime) -> None:
    runtime.load()

    assert runtime.is_healthy() is True


def test_load_wraps_loader_exception_in_heart_load_error(tmp_path: Path) -> None:
    def _raising_loader(model_path: Path) -> tuple[object, object]:
        raise RuntimeError("boom")

    runtime = MlxHeartRuntime(
        tmp_path, context_tokens=100, max_output_tokens=10, loader=_raising_loader
    )

    with pytest.raises(HeartLoadError, match="boom"):
        runtime.load()


def test_token_count_delegates_to_tokenizer(runtime: MlxHeartRuntime) -> None:
    runtime.load()

    assert runtime.token_count("hello") == 5


def test_unload_drops_model_and_tokenizer(runtime: MlxHeartRuntime) -> None:
    runtime.load()

    runtime.unload()

    assert runtime.is_healthy() is False


def test_load_generate_and_token_count_all_run_on_the_same_dedicated_thread(
    monkeypatch: pytest.MonkeyPatch, runtime: MlxHeartRuntime
) -> None:
    """The load-bearing property issue #55's Mac Mini M4 run found broken: MLX's
    Metal backend binds a model's GPU stream to whichever thread first touched it,
    so `generate()`/`token_count()` must land on the exact same thread `load()`
    did — regardless of which thread (here: the main test thread, standing in for
    the event loop or one of `anyio`'s own worker threads) called into each one.
    """
    threads_seen: set[int] = set()

    class _ThreadRecordingTokenizer(_FakeTokenizer):
        def encode(self, text: str) -> list[int]:
            threads_seen.add(threading.get_ident())
            return super().encode(text)

    def _recording_loader(model_path: Path) -> tuple[object, object]:
        threads_seen.add(threading.get_ident())
        return object(), _ThreadRecordingTokenizer()

    def _fake_generate(model: object, tokenizer: object, **kwargs: Any) -> str:
        threads_seen.add(threading.get_ident())
        return "generated text"

    monkeypatch.setitem(sys.modules, "mlx_lm", SimpleNamespace(generate=_fake_generate))
    runtime = MlxHeartRuntime(
        Path("/unused"),
        context_tokens=100,
        max_output_tokens=10,
        loader=_recording_loader,
    )

    runtime.load()
    runtime.token_count("hello")
    runtime.generate("hi", max_tokens=5)

    # Every call above ran on this (the main test) thread's perspective, but all
    # three must have landed on the *one* dedicated MLX thread underneath —
    # never the calling thread itself, and never a different thread each time.
    assert len(threads_seen) == 1
    assert threading.get_ident() not in threads_seen


def test_unload_tears_down_the_dedicated_thread_for_a_later_reload(
    runtime: MlxHeartRuntime,
) -> None:
    """`unload()` must not leave a stale dedicated thread behind — a later
    `load()` gets a fresh one rather than resuming a thread whose MLX state
    `unload()` just cleared.
    """
    runtime.load()
    first_executor = runtime._executor
    assert first_executor is not None

    runtime.unload()
    executor_after_unload = runtime._executor
    assert executor_after_unload is None
    with pytest.raises(RuntimeError):
        first_executor.submit(lambda: None).result()

    runtime.load()
    second_executor = runtime._executor
    assert second_executor is not None
    assert second_executor is not first_executor


def test_generate_calls_mlx_lm_generate_and_returns_its_result(
    monkeypatch: pytest.MonkeyPatch, runtime: MlxHeartRuntime
) -> None:
    calls: list[dict[str, Any]] = []

    def _fake_generate(model: object, tokenizer: object, **kwargs: Any) -> str:
        calls.append(kwargs)
        return "generated text"

    monkeypatch.setitem(sys.modules, "mlx_lm", SimpleNamespace(generate=_fake_generate))
    runtime.load()

    result = runtime.generate("hi", max_tokens=5)

    assert result == "generated text"
    assert calls == [{"prompt": "hi", "max_tokens": 5, "verbose": False}]


def test_generate_wraps_backend_exception_in_heart_error(
    monkeypatch: pytest.MonkeyPatch, runtime: MlxHeartRuntime
) -> None:
    def _raising_generate(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("backend exploded")

    monkeypatch.setitem(
        sys.modules, "mlx_lm", SimpleNamespace(generate=_raising_generate)
    )
    runtime.load()

    with pytest.raises(HeartError, match="backend exploded"):
        runtime.generate("hi")


def test_unload_clears_mlx_cache_when_mlx_core_is_available(
    monkeypatch: pytest.MonkeyPatch, runtime: MlxHeartRuntime
) -> None:
    calls: list[str] = []
    stub_core = SimpleNamespace(clear_cache=lambda: calls.append("cleared"))
    monkeypatch.setitem(sys.modules, "mlx", SimpleNamespace(core=stub_core))
    monkeypatch.setitem(sys.modules, "mlx.core", stub_core)
    runtime.load()

    runtime.unload()

    assert calls == ["cleared"]


def test_unload_tolerates_missing_mlx_core(
    monkeypatch: pytest.MonkeyPatch, runtime: MlxHeartRuntime
) -> None:
    monkeypatch.setitem(sys.modules, "mlx", None)
    runtime.load()

    runtime.unload()  # must not raise

    assert runtime.is_healthy() is False


def test_generate_applies_the_tokenizers_chat_template_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tokenizer = _FakeChatTokenizer()

    def _loader(model_path: Path) -> tuple[object, object]:
        return object(), tokenizer

    runtime = MlxHeartRuntime(
        tmp_path, context_tokens=100, max_output_tokens=10, loader=_loader
    )
    calls: list[dict[str, Any]] = []

    def _fake_generate(model: object, tokenizer: object, **kwargs: Any) -> str:
        calls.append(kwargs)
        return "idle"

    monkeypatch.setitem(sys.modules, "mlx_lm", SimpleNamespace(generate=_fake_generate))
    runtime.load()

    runtime.generate("hi", max_tokens=5)

    assert calls[0]["prompt"] == "<chat>hi</chat>"
    assert tokenizer.last_kwargs == {"enable_thinking": False}


def test_generate_skips_the_chat_template_when_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _loader(model_path: Path) -> tuple[object, object]:
        return object(), _FakeChatTokenizer()

    runtime = MlxHeartRuntime(
        tmp_path,
        context_tokens=100,
        max_output_tokens=10,
        loader=_loader,
        apply_chat_template=False,
    )
    calls: list[dict[str, Any]] = []

    def _fake_generate(model: object, tokenizer: object, **kwargs: Any) -> str:
        calls.append(kwargs)
        return "idle"

    monkeypatch.setitem(sys.modules, "mlx_lm", SimpleNamespace(generate=_fake_generate))
    runtime.load()

    runtime.generate("hi", max_tokens=5)

    assert calls[0]["prompt"] == "hi"


def test_generate_falls_back_when_the_tokenizer_rejects_enable_thinking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The rarer shape: a tokenizer whose `apply_chat_template` doesn't accept
    `enable_thinking` (or any extra kwarg) at all raises `TypeError` on the first
    attempt, and `_render_prompt` retries without it rather than failing the whole
    `generate()` call.
    """

    def _loader(model_path: Path) -> tuple[object, object]:
        return object(), _FakeLegacyChatTokenizer()

    runtime = MlxHeartRuntime(
        tmp_path, context_tokens=100, max_output_tokens=10, loader=_loader
    )
    calls: list[dict[str, Any]] = []

    def _fake_generate(model: object, tokenizer: object, **kwargs: Any) -> str:
        calls.append(kwargs)
        return "idle"

    monkeypatch.setitem(sys.modules, "mlx_lm", SimpleNamespace(generate=_fake_generate))
    runtime.load()

    runtime.generate("hi", max_tokens=5)

    assert calls[0]["prompt"] == "<chat>hi</chat>"


def test_default_loader_calls_mlx_lm_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    def _fake_load(model_path: str) -> tuple[str, str]:
        calls.append(model_path)
        return "model", "tokenizer"

    monkeypatch.setitem(sys.modules, "mlx_lm", SimpleNamespace(load=_fake_load))

    result = _default_loader(tmp_path)

    assert result == ("model", "tokenizer")
    assert calls == [str(tmp_path)]
