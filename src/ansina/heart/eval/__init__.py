"""The Heart tick-decision bench harness. See issue #53.

Nothing calls `HeartRuntime.generate()` outside a unit test before this package — it is
what turns "the tick loop exists" (issue #11) into "the tick loop works, measured
against a labelled fixture set." `fixtures.py` loads + validates the fixture set,
`runner.py` runs it against any `HeartRuntime` (a fake in the unit suite, the real MLX
adapter on the Mac Mini M4), and `report.py` renders the result as JSON + markdown and
computes the pass/fail gate `docs/heart/bench/` reports are judged against.

`__main__.py` (`python -m ansina.heart.eval`) is the real-model entry point — never
invoked in CI, since no MLX adapter is viable on either CI leg; it also uploads the
report pair it just wrote to the S3-compatible report bucket when `[telemetry.s3]
enabled = true` (`storage.py`, issue #59). `publish.py` (`python -m ansina.heart.eval.
publish`) is the separate backlog-migration command that mirrors the whole local
`docs/heart/bench/`/`docs/heart/soak/` corpus onto that same bucket.
"""

from __future__ import annotations
