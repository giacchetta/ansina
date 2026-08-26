"""In-process Heart runtime: the ≤4B, 8k-context always-on model. See issue #10.

`HeartRuntime` is the port; `build_heart_runtime()` probes the host and returns the
one viable adapter (or raises `HeartUnavailableError` loudly). Nothing calls this yet
— the autonomic tick loop (issue #11) is the first consumer. `create_app()`
(`ansina.api.app`) owns construction, load, and unload, the same way it owns `Database`.
"""

from ansina.heart.runtime import (
    HeartContextOverflowError,
    HeartLoadError,
    HeartNotLoadedError,
    HeartRuntime,
    HeartUnavailableError,
)
from ansina.heart.selection import build_heart_runtime

__all__ = [
    "HeartContextOverflowError",
    "HeartLoadError",
    "HeartNotLoadedError",
    "HeartRuntime",
    "HeartUnavailableError",
    "build_heart_runtime",
]
