"""In-process Heart runtime: the ≤4B, 8k-context always-on model. See issue #10.

`HeartRuntime` is the port; `build_heart_runtime()` probes the host and returns the one
viable adapter (or raises `HeartUnavailableError` loudly). `heart.tick.TickLoop` (issue
#11) is the first and only consumer of `generate()` — it calls the Heart on a fixed
cadence to decide idle/act/escalate. `create_app()` (`ansina.api.app`) owns
construction, load/unload, and tick-loop start/stop, the same way it owns `Database`.
"""

from ansina.heart.runtime import (
    HeartContextOverflowError,
    HeartLoadError,
    HeartNotLoadedError,
    HeartRuntime,
    HeartUnavailableError,
)
from ansina.heart.selection import build_heart_runtime
from ansina.heart.tick import TickController, TickLifecycle, TickLoop, build_tick_loop

__all__ = [
    "HeartContextOverflowError",
    "HeartLoadError",
    "HeartNotLoadedError",
    "HeartRuntime",
    "HeartUnavailableError",
    "TickController",
    "TickLifecycle",
    "TickLoop",
    "build_heart_runtime",
    "build_tick_loop",
]
