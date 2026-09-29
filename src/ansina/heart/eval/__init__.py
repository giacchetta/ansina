"""The Heart tick-decision bench harness. See issue #53.

Nothing calls `HeartRuntime.generate()` outside a unit test before this package — it is
what turns "the tick loop exists" (issue #11) into "the tick loop works, measured
against a labelled fixture set." `fixtures.py` loads + validates the fixture set,
`runner.py` runs it against any `HeartRuntime` (a fake in the unit suite, the real MLX
adapter on the Mac Mini M4), and `report.py` renders the result as JSON + markdown and
computes the pass/fail gate `docs/heart/bench/` reports are judged against.

`__main__.py` (`python -m ansina.heart.eval`) is the real-model entry point — never
invoked in CI, since no MLX adapter is viable on either CI leg.
"""

from __future__ import annotations
