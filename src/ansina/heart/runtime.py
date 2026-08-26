"""`HeartRuntime` — the port every in-process Heart adapter implements. See issue #10.

The Heart is a ≤4B, 8k-context model running in-process (no subprocess, no HTTP hop —
`docs/architecture/blueprint.md` §3). `HeartRuntime` is a `Protocol` (structural, not a
base class) so an adapter needs no import-time coupling to this module; `mlx.py` is the
only adapter this milestone ships, but the port stays adapter-agnostic on purpose — a
second adapter (tracked in a follow-up issue) is meant to be a drop-in.

`generate()` is synchronous and blocking: it does real in-process GPU/CPU work. Callers
on an event loop (the autonomic tick loop, issue #11) must run it via
`anyio.to_thread.run_sync`, never await it directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar, Protocol, runtime_checkable

from ansina.errors import HeartError
from ansina.logging import get_logger

logger = get_logger(__name__)


class HeartUnavailableError(HeartError):
    """No viable `HeartRuntime` adapter for this host. See `ansina.heart.selection`."""

    code: ClassVar[str] = "ansina.heart.unavailable"


class HeartLoadError(HeartError):
    """The selected adapter failed to load its model."""

    code: ClassVar[str] = "ansina.heart.load_failed"


class HeartNotLoadedError(HeartError):
    """`generate()` (or another loaded-only call) was made before `load()`."""

    code: ClassVar[str] = "ansina.heart.not_loaded"


class HeartContextOverflowError(HeartError):
    """A prompt plus its requested `max_tokens` would exceed `context_tokens`.

    The 8k budget is a hard constraint (blueprint §3), not a target — this refuses
    rather than silently truncating, because the blueprint is explicit that a
    crowded context is exactly what a 4B model's quality can't tolerate.
    """

    code: ClassVar[str] = "ansina.heart.context_overflow"


@runtime_checkable
class HeartRuntime(Protocol):
    """The four operations issue #10 names, plus a `Readiness`-shaped health check."""

    @property
    def context_tokens(self) -> int:
        """The hard token budget every prompt built for this runtime must fit."""
        ...

    def load(self) -> None:
        """Load the model into memory. Call once, before any `generate()` call."""
        ...

    def generate(self, prompt: str, *, max_tokens: int | None = None) -> str:
        """Blocking. Raises `HeartNotLoadedError` before `load()`, and
        `HeartContextOverflowError` if `prompt` plus `max_tokens` would exceed
        `context_tokens`.
        """
        ...

    def token_count(self, text: str) -> int:
        """The number of tokens `text` would consume in this runtime's tokenizer."""
        ...

    def unload(self) -> None:
        """Release the model and any backend memory. Safe to call when not loaded."""
        ...

    def is_healthy(self) -> bool:
        """`True` iff loaded and a trivial round-trip succeeds. Never raises — same
        contract as `Database.is_healthy` (`ansina.storage.database`), since this
        feeds a `Readiness` check the same way.
        """
        ...


class BaseHeartRuntime(ABC):
    """Shared state-guard and context-budget logic for every `HeartRuntime` adapter.

    Subclasses implement the four `_*` backend hooks; this class owns the loaded/
    unloaded guard and the budget refusal so that logic lives in exactly one place
    rather than being re-implemented (and possibly re-relaxed) per adapter.
    """

    def __init__(self, *, context_tokens: int, max_output_tokens: int) -> None:
        self._context_tokens = context_tokens
        self._max_output_tokens = max_output_tokens
        self._loaded = False

    @property
    def context_tokens(self) -> int:
        return self._context_tokens

    def load(self) -> None:
        if self._loaded:
            return
        self._load_backend()
        self._loaded = True

    def generate(self, prompt: str, *, max_tokens: int | None = None) -> str:
        if not self._loaded:
            raise HeartNotLoadedError("generate() called before load()")
        budget = max_tokens if max_tokens is not None else self._max_output_tokens
        used = self.token_count(prompt) + budget
        if used > self._context_tokens:
            raise HeartContextOverflowError(
                f"prompt ({used - budget} tokens) plus max_tokens ({budget}) = "
                f"{used} tokens exceeds the {self._context_tokens}-token budget"
            )
        return self._generate(prompt, budget)

    def token_count(self, text: str) -> int:
        return self._token_count(text)

    def unload(self) -> None:
        if not self._loaded:
            return
        self._unload_backend()
        self._loaded = False

    def is_healthy(self) -> bool:
        if not self._loaded:
            return False
        try:
            self._token_count("")
        except HeartError:
            return False
        return True

    @abstractmethod
    def _load_backend(self) -> None: ...

    @abstractmethod
    def _generate(self, prompt: str, max_tokens: int) -> str: ...

    @abstractmethod
    def _token_count(self, text: str) -> int: ...

    @abstractmethod
    def _unload_backend(self) -> None: ...
