"""Capability probe + `HeartRuntime` factory. See issue #10.

With a single adapter (MLX, Apple-Silicon-only — a portable fallback is deferred to a
follow-up issue; see `ansina.heart.adapters`), this module *is* the "fail loudly, not
silently degrade" requirement: any host MLX can't run on gets a `HeartUnavailableError`
naming exactly which condition failed, never a silent no-op or a swap to some other
backend.
"""

from __future__ import annotations

import importlib.util
import platform
import sys
from collections.abc import Callable

from ansina.config.settings import HeartSettings, Settings
from ansina.heart.adapters.mlx import MlxHeartRuntime
from ansina.heart.models import ResolvedModel, resolve_model
from ansina.heart.runtime import HeartRuntime, HeartUnavailableError
from ansina.logging import get_logger

logger = get_logger(__name__)

Resolver = Callable[[HeartSettings], ResolvedModel]

_FOLLOW_UP_NOTE = (
    "no non-MLX HeartRuntime adapter exists yet — the llama-cpp-python fallback was "
    "deferred to a follow-up issue (no llama-cpp-python-compatible GPU was available "
    "to prove it against at the time issue #10 was implemented)"
)


def _mlx_viable() -> tuple[bool, str]:
    """`(viable, reason)` — `reason` always states the host facts, whether or not
    this returns `True`, so a refusal message can quote exactly what failed.
    """
    # Read through a local `str`-typed variable rather than comparing `sys.platform`
    # directly: mypy special-cases direct `sys.platform == "..."` comparisons as
    # platform-conditional code and, since CI runs this on both `ubuntu-24.04` and
    # `macos-26`, would otherwise statically mark one OS leg's branch "unreachable"
    # depending on whichever platform mypy itself runs on — the opposite of what this
    # function needs, which is to evaluate the *runtime* host, not the type-checker's.
    current_platform: str = sys.platform
    machine = platform.machine()
    if current_platform != "darwin":
        return False, f"platform is {current_platform!r}, not 'darwin'"
    if machine != "arm64":
        return False, f"machine is {machine!r}, not 'arm64' (Apple Silicon)"
    # `mlx` now ships manylinux wheels too, so import success alone doesn't imply a
    # usable Metal backend — the platform/machine check above plus `mlx_lm`
    # importability together are what "viable" means here.
    if importlib.util.find_spec("mlx_lm") is None:
        return False, (
            "mlx_lm is not installed — run `uv sync --extra mlx` on Apple Silicon"
        )
    return True, "darwin/arm64 with mlx_lm installed"


def build_heart_runtime(
    settings: Settings, *, resolver: Resolver = resolve_model
) -> HeartRuntime:
    """Probe the host, then build the one viable adapter (or raise loudly).

    `resolver` defaults to `ansina.heart.models.resolve_model`; tests inject a fake to
    avoid ever hitting the network or the filesystem for a real model.
    """
    heart_settings = settings.heart
    viable, reason = _mlx_viable()

    if heart_settings.runtime == "mlx" and not viable:
        raise HeartUnavailableError(
            f"heart.runtime is explicitly 'mlx' but MLX is not viable on this host "
            f"({reason}); {_FOLLOW_UP_NOTE}"
        )
    if heart_settings.runtime == "auto" and not viable:
        raise HeartUnavailableError(
            f"no viable HeartRuntime adapter for this host ({reason}); "
            f"{_FOLLOW_UP_NOTE}"
        )

    logger.info("selected heart runtime", extra={"runtime": "mlx", "reason": reason})
    resolved = resolver(heart_settings)
    return MlxHeartRuntime(
        resolved.path,
        context_tokens=heart_settings.context_tokens,
        max_output_tokens=heart_settings.max_output_tokens,
    )
