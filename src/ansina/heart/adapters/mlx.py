"""MLX adapter for `HeartRuntime` — the primary, Apple-Silicon-only backend.

See issue #10. `_default_loader` imports `mlx_lm` inside its own body, never at module
scope, so this module stays importable without the `ansina[mlx]` extra installed —
`ansina.heart.selection`'s capability probe is what decides whether `MlxHeartRuntime`
is ever constructed in the first place.

**Every real MLX call for one instance runs on a single dedicated thread** (issue
#55's own Mac Mini M4 acceptance run, the first time this adapter's `generate()` was
ever exercised through `anyio.to_thread.run_sync` rather than called synchronously
from `heart.eval`'s bench harness or from `load()`'s own direct, unoffloaded call in
`api/app.py`'s lifespan): MLX's Metal backend binds a model's GPU stream to whichever
OS thread first touched it, and `anyio.to_thread.run_sync`'s own worker-thread pool
makes no guarantee that a later `generate()` call lands on that same thread — it
failed every time with `RuntimeError: There is no Stream(gpu, 1) in current thread.`,
confirmed live. `_run_on_mlx_thread()` is the fix: a lazily-created, single-worker
`concurrent.futures.ThreadPoolExecutor` that every one of the four `_*` backend hooks
below submits its real work to and blocks on — so `load()`/`generate()`/
`token_count()`/`unload()` always run on the *same* thread for this instance's whole
lifetime, regardless of which thread (the event loop, an anyio worker, `heart.eval`'s
main thread) called in. Orthogonal to, and fully compatible with, the caller's own
`anyio.to_thread.run_sync` offload (`heart/runtime.py`'s module docstring): that is
what keeps a blocking call off the *event loop*; this is what keeps MLX's own
thread-local state consistent underneath it.
"""

from __future__ import annotations

import concurrent.futures
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from ansina.errors import HeartError
from ansina.heart.runtime import BaseHeartRuntime, HeartLoadError
from ansina.logging import get_logger

logger = get_logger(__name__)

_T = TypeVar("_T")

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
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None

    def _run_on_mlx_thread(self, fn: Callable[[], _T]) -> _T:
        """Runs `fn` on this instance's dedicated single worker thread, blocking
        until it completes — see this module's own docstring for why. Created
        lazily (first call is always from `_load_backend`) and torn down in
        `_unload_backend`, so a fresh `load()` after `unload()` gets a fresh thread
        rather than reusing one whose MLX state was already cleared.
        """
        if self._executor is None:
            self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        return self._executor.submit(fn).result()

    def _load_backend(self) -> None:
        def _load() -> None:
            try:
                self._model, self._tokenizer = self._loader(self._model_path)
            except Exception as exc:  # mlx's own exception types aren't ours to name
                raise HeartLoadError(
                    f"mlx failed to load model at {str(self._model_path)!r}: {exc}"
                ) from exc

        self._run_on_mlx_thread(_load)

    def _render_prompt(self, prompt: str) -> str:
        """Wraps `prompt` as a single user turn through the loaded tokenizer's own
        chat template (issue #53) — every model on the bench ladder is chat/
        instruct-tuned, and a raw, untemplated prompt measurably makes one continue
        the prompt as free text rather than answer it (100% parse-fallback on the
        first real bench run, `docs/heart/bench/`'s earliest report — published to
        the report bucket as of issue #59, `kind=bench/dt=2026-09-29/...`). A
        tokenizer with no `apply_chat_template` (or `HeartSettings.apply_chat_template
        = False`, the escape hatch for a future base/non-instruct model) falls back to
        the raw prompt unchanged — the pre-#53 behavior.

        Also passes `enable_thinking=False` (the Qwen3-family template's own kwarg to
        pre-close the `<think>` block rather than leave it open for the model to
        reason into) — a second, equally measured finding from that same first
        chat-templated run: with reasoning left on, 18/24 fixtures never reached a
        final answer within `max_output_tokens` at all, and every run that did still
        blew the p95-latency gate. A tick that reasons for multiple seconds before a
        3-way classification defeats the Heart's whole "fast, narrow, autonomic"
        design (blueprint §3), regardless of which model produces it — this is a
        latency/budget problem, not just a parsing one. Passed unconditionally: a
        template that has no concept of `enable_thinking` simply never references it
        in its own Jinja logic, so the kwarg is inert rather than erroring — verified
        directly against the Qwen3.5 tokenizer actually on the bench ladder. `TypeError`
        is still caught, for the rarer case of a tokenizer whose `apply_chat_template`
        doesn't forward arbitrary keyword arguments to the template context at all.

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
        messages = [{"role": "user", "content": prompt}]
        try:
            rendered: str = render(
                messages,
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=False,
            )
        except TypeError:
            rendered = render(messages, tokenize=False, add_generation_prompt=True)
        return rendered

    def _generate(self, prompt: str, max_tokens: int) -> str:
        def _run() -> str:
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

        return self._run_on_mlx_thread(_run)

    def _token_count(self, text: str) -> int:
        def _count() -> int:
            tokens: list[int] = self._tokenizer.encode(text)
            return len(tokens)

        return self._run_on_mlx_thread(_count)

    def _unload_backend(self) -> None:
        def _unload() -> None:
            self._model = None
            self._tokenizer = None
            try:
                import mlx.core as mx
            except ImportError:
                return
            mx.clear_cache()

        self._run_on_mlx_thread(_unload)
        # Torn down, not reused — a later `load()` creates a fresh executor (and so
        # a fresh dedicated thread) rather than resuming one whose MLX state this
        # call just cleared.
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None
