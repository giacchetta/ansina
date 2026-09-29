"""MLX adapter for `HeartRuntime` — the primary, Apple-Silicon-only backend.

See issue #10. `_default_loader` imports `mlx_lm` inside its own body, never at module
scope, so this module stays importable without the `ansina[mlx]` extra installed —
`ansina.heart.selection`'s capability probe is what decides whether `MlxHeartRuntime`
is ever constructed in the first place.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ansina.errors import HeartError
from ansina.heart.runtime import BaseHeartRuntime, HeartLoadError
from ansina.logging import get_logger

logger = get_logger(__name__)

# `(model_path) -> (model, tokenizer)`. Deliberately `Any`, not `mlx_lm`'s real
# `nn.Module`/`TokenizerWrapper` types: `mlx.*`/`mlx_lm.*` are under `ignore_missing_
# imports` below (the extra isn't installed in CI), so importing those types here
# would still resolve to `Any` at check time and would break the moment the extra
# genuinely isn't installed. Kept as a named alias so it reads as a documented seam,
# not a stray untyped parameter.
Loader = Callable[[Path], tuple[Any, Any]]


def _default_loader(model_path: Path) -> tuple[Any, Any]:
    from mlx_lm import load

    # `load()`'s real return type is `tuple[model, tokenizer] | tuple[model,
    # tokenizer, config]`, keyed on its own `return_config` kwarg — never passed
    # here, so the third element never actually appears, but mypy can't narrow a
    # union on an unpassed default. Indexing (rather than assigning the whole
    # result to a `tuple[Any, Any]`-typed variable) is valid against either arm of
    # that union, so it stays correct even if a future `mlx_lm` release adds a
    # fourth return element.
    result = load(str(model_path))
    return result[0], result[1]


class MlxHeartRuntime(BaseHeartRuntime):
    """MLX (`mlx-lm`) backend — in-process, Apple Silicon / unified memory."""

    def __init__(
        self,
        model_path: Path,
        *,
        context_tokens: int,
        max_output_tokens: int,
        loader: Loader = _default_loader,
        apply_chat_template: bool = True,
    ) -> None:
        super().__init__(
            context_tokens=context_tokens, max_output_tokens=max_output_tokens
        )
        self._model_path = model_path
        self._loader = loader
        self._apply_chat_template = apply_chat_template
        self._model: Any = None
        self._tokenizer: Any = None

    def _load_backend(self) -> None:
        try:
            self._model, self._tokenizer = self._loader(self._model_path)
        except Exception as exc:  # mlx's own exception types aren't ours to name
            raise HeartLoadError(
                f"mlx failed to load model at {str(self._model_path)!r}: {exc}"
            ) from exc

    def _render_prompt(self, prompt: str) -> str:
        """Wraps `prompt` as a single user turn through the loaded tokenizer's own
        chat template (issue #53) — every model on the bench ladder is chat/
        instruct-tuned, and a raw, untemplated prompt measurably makes one continue
        the prompt as free text rather than answer it (100% parse-fallback on the
        first real bench run, `docs/heart/bench/`'s earliest report). A tokenizer
        with no `apply_chat_template` (or `HeartSettings.apply_chat_template =
        False`, the escape hatch for a future base/non-instruct model) falls back to
        the raw prompt unchanged — the pre-#53 behavior.

        Deliberately not reflected in `_token_count`: the template adds a small,
        roughly constant per-call overhead (a handful of special tokens), not
        proportional to snapshot content, so `heart.tick.snapshot.build_prompt`'s
        per-item budgeting (which calls `token_count` on raw item text, not a
        templated turn) stays a close, if slightly conservative-in-the-other-
        direction, proxy for what the model actually consumes.
        """
        if not self._apply_chat_template:
            return prompt
        render = getattr(self._tokenizer, "apply_chat_template", None)
        if render is None:
            return prompt
        rendered: str = render(
            [{"role": "user", "content": prompt}],
            tokenize=False,
            add_generation_prompt=True,
        )
        return rendered

    def _generate(self, prompt: str, max_tokens: int) -> str:
        from mlx_lm import generate

        try:
            result: str = generate(
                self._model,
                self._tokenizer,
                prompt=self._render_prompt(prompt),
                max_tokens=max_tokens,
                verbose=False,
            )
        except Exception as exc:  # mlx's own exception types aren't ours to name
            raise HeartError(f"mlx failed to generate: {exc}") from exc
        return result

    def _token_count(self, text: str) -> int:
        tokens: list[int] = self._tokenizer.encode(text)
        return len(tokens)

    def _unload_backend(self) -> None:
        self._model = None
        self._tokenizer = None
        try:
            import mlx.core as mx
        except ImportError:
            return
        mx.clear_cache()
