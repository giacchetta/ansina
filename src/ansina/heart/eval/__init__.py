"""The Heart bench harness: the tick-decision suite (issue #53) and the gated
request-triage experiment (issue #13).

Nothing called `HeartRuntime.generate()` outside a unit test before issue #53 — it is
what turns "the tick loop exists" (issue #11) into "the tick loop works, measured
against a labelled fixture set." `fixtures.py` loads + validates both suites' fixture
sets (sharing its line-reading/validation machinery between them), `metrics.py` holds
the label-agnostic measurement primitives both suites build on, `runner.py`/`report.py`
run the tick suite and render its result, and `triage_runner.py`/`triage_report.py`
mirror that shape for the triage suite — each renders JSON + markdown and computes its
own pass/fail gate. The triage port itself (`heart.triage`) is wired into nothing
beyond this bench — see that module's own docstring.

`__main__.py` (`python -m ansina.heart.eval`, `--suite {tick,triage}`) is the
real-model entry point for both — never invoked in CI, since no MLX adapter is viable
on either CI leg; it also uploads the report pair it just wrote to the S3-compatible
report bucket when `[telemetry.s3] enabled = true` (`storage.py`, issue #59), for
either suite. `publish.py` (`python -m ansina.heart.eval.publish`) is the separate
backlog-migration command that mirrors the whole local `docs/heart/bench/`/`docs/heart/
soak/` corpus onto that same bucket.
"""

from __future__ import annotations
