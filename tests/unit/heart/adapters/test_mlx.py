from __future__ import annotations

import sys
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
