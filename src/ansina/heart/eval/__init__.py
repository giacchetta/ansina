"""The Heart bench harness: the tick-decision suite (issue #53), the gated
request-triage experiment (issue #13), and the gated context-curation experiment
(issue #14).

Nothing called `HeartRuntime.generate()` outside a unit test before issue #53 — it is
what turns "the tick loop exists" (issue #11) into "the tick loop works, measured
against a labelled fixture set." `fixtures.py` loads + validates all three suites'
fixture sets (sharing its line-reading/validation machinery between them),
`metrics.py` holds the label-agnostic measurement primitives the tick/triage suites
build on, `runner.py`/`report.py` run the tick suite and render its result, and
`triage_runner.py`/`triage_report.py` mirror that shape for the triage suite — each
renders JSON + markdown and computes its own pass/fail gate. `curation_runner.py`/
`curation_report.py` do the same for the curation suite, with one structural
difference: it is the one suite that is genuinely async, since it drives real
`BrainProvider.stream()` calls (issue #12) rather than only `HeartRuntime.generate()`.
Neither the triage port (`heart.triage`) nor the curation port (`heart.curation`) is
wired into anything beyond this bench — see each module's own docstring.

`__main__.py` (`python -m ansina.heart.eval`, `--suite {tick,triage,curation}`) is the
real-model entry point for all three — never invoked in CI, since no MLX adapter is
viable on either CI leg; it also uploads the report pair it just wrote to the
S3-compatible report bucket when `[telemetry.s3] enabled = true` (`storage.py`, issue
#59), for any suite. `publish.py` (`python -m ansina.heart.eval.publish`) is the
separate backlog-migration command that mirrors the whole local `docs/heart/bench/`/
`docs/heart/soak/` corpus onto that same bucket.
"""

from __future__ import annotations
