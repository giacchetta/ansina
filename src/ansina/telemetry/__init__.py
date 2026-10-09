"""Flag-gated local telemetry producer: a rotated RSS/tick/decision sample stream
plus a redacted log mirror, files only, never SQLite, never uploaded. See issue
#61. Issue #62's Vector sidecar ships/tails these files; issue #59's bucket
pipeline is a separate, already-shipped concern this package never touches.

**This package's own `__init__` deliberately re-exports only `rotation.py` and
`log_mirror.py` — never `sampler.py`.** `sampler.py` needs `ansina.heart.tick.loop
.TickController`, which itself imports `ansina.logging`. `ansina.logging.setup`
(this package's own consumer, for the log mirror) imports `ansina.telemetry.
log_mirror` directly — and importing *any* name from this package first runs this
`__init__.py` in full. If this `__init__` eagerly re-exported `sampler.py` too,
that chain would become: `ansina.logging.__init__` (mid-import, since it itself
imports `.setup`) -> `ansina.logging.setup` -> `ansina.telemetry` (this file) ->
`ansina.telemetry.sampler` -> `ansina.heart.tick.loop` -> `from ansina.logging
import get_logger`, reaching back into `ansina.logging` while it is still
mid-import — a real circular import, `ImportError: cannot import name 'get_logger'
from partially initialized module`. Keeping `sampler.py` out of this `__init__`
avoids it entirely: `ansina.api.app` (the sampler's only consumer) imports
`ansina.telemetry.sampler` directly, as a submodule, the same deferred-import
discipline `heart/journal.py`'s own module docstring documents in detail for its
analogous `heart.tick.decision` cycle.
"""

from ansina.telemetry.log_mirror import TelemetryLogHandler
from ansina.telemetry.rotation import SpooledRotatingWriter

__all__ = [
    "SpooledRotatingWriter",
    "TelemetryLogHandler",
]
