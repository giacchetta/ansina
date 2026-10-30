"""`OpencodeAction` — the Heart's fixed, enumerable set of bounded interventions on
the OpenCode executor. See issue #79.

A deliberately closed vocabulary: the Heart's own model output never becomes an
argv element, a prompt body, or a file path anywhere in this codebase (issue #79's
own acceptance criterion). `heart.tick.opencode_intervention_handler
.DeterministicInterventionPolicy` selects a member of this enum plus an existing,
already-persisted `opencode_runs.id` from real state (`heart.tick.sources
.opencode_state.OpencodeStateProbe`'s own signals); the Heart's model output is
consumed only as "act / don't act" (`TickDecision.ACT`), never as the action or the
target. `ansina.opencode.service.OpencodeRunService.apply_intervention()` is the one
seam a caller ever hands an `(run_id, action)` pair to — see that method's own
docstring.

`build_retry_request`/`build_nudge_request` below are the two pure, synchronous
request builders `apply_intervention()` uses for `OpencodeAction.RETRY`/`.NUDGE` —
kept here, not in `service.py`, so they're unit-testable with no async/repository
fixture at all, and so the "never model-authored text" invariant has one small,
easily-audited home: both ever only derive a `RunRequest` from an already-persisted
`OpencodeRun`'s own fields (`RETRY` reuses its `prompt` verbatim — the same prompt a
human or route already submitted once, not new text) or a fixed, code-generated
constant (`NUDGE`'s own continuation — see `_NUDGE_CONTINUATION_PROMPT`). Neither
function accepts a free-text parameter.

`OpencodeAction.COMPACT` exists in the closed enum and has a real dispatch branch in
`apply_intervention()` (ships the verified `UNSUPPORTED` outcome). Verified for real
against an installed OpenCode v2.1.4 binary at implementation time: a compact entry
point does exist — `POST /api/session/{id}/compact` ("Compact session," confirmed
via the real `/openapi.json` the binary's own `opencode serve` exposes) — but there
is no CLI subcommand or flag for it anywhere (`opencode --help` and every
subcommand's own `--help` were checked directly; `opencode session --help` lists
`list`/`delete`/`export`/`import` only). `apply_intervention()`'s `COMPACT` branch is
therefore unreachable from `OpencodeCliRunner`'s one-shot `opencode run` subprocess
model — reaching the real endpoint would need a second, server-based adapter behind
`AgentRunner`, which that Protocol's own module docstring already anticipates as a
pure, future append ("a future `opencode serve` + httpx adapter"), not something this
issue builds. `DeterministicInterventionPolicy` never *selects* `COMPACT` either way:
no existing `OpencodeStateProbe` signal reports context-pressure today, and inventing
one ahead of a real signal would be exactly the kind of unverified assumption this
codebase's "verify, don't assume" discipline (the Vector-0.45.x pin, the v1-vs-v2
`--version` shape finding, `ansina.opencode.events`' own intentionally-empty
`RunEventType` lookup table) already refuses elsewhere. A later, bench-gated issue
(#15, "Heart as structured extractor") is the named path to a model-chosen policy
that might pick a real signal for this rung.
"""

from __future__ import annotations

from enum import StrEnum

from ansina.opencode.events import RunRequest
from ansina.opencode.repository import OpencodeRun


class OpencodeAction(StrEnum):
    """The Heart's closed set of bounded interventions on a specific run (or, for
    `PAUSE_INTAKE`, on the executor as a whole). Never free-form — see module
    docstring.
    """

    RETRY = "retry"
    NUDGE = "nudge"
    COMPACT = "compact"
    PAUSE_INTAKE = "pause_intake"
    ESCALATE_TO_BRAIN = "escalate_to_brain"


# A fixed, code-generated continuation — never model-authored, per the issue's own
# line. Deliberately generic (names no tool, no file, no decision) since this text
# is submitted as a fresh one-shot `opencode run` against the stalled run's own
# persisted session (`--session`), the only mechanism `AgentRunner` exposes:
# `OpencodeCliRunner.run()` is one-shot per call with no interactive stdin, so there
# is no way to "nudge" a still-running process directly — this is a fresh call
# that resumes its session instead.
_NUDGE_CONTINUATION_PROMPT = (
    "Continue from where you left off. If you are stuck or waiting on something, "
    "state plainly what you need."
)


def build_retry_request(run: OpencodeRun) -> RunRequest:
    """A fresh attempt at `run`'s own prompt — not a continuation (no `session_id`),
    since a retry starts over rather than resuming a session that already failed or
    timed out. Reuses `run.prompt`/`run.model`/`run.agent` verbatim; nothing here
    accepts or derives from any other input.
    """
    return RunRequest(prompt=run.prompt, model=run.model, agent=run.agent)


def build_nudge_request(run: OpencodeRun) -> RunRequest:
    """A fixed, code-generated continuation against `run`'s own `session_id` — see
    `_NUDGE_CONTINUATION_PROMPT`'s own docstring for why this is a fresh call, not a
    message sent to the still-running process. `model`/`agent` are reused the same
    way `build_retry_request` reuses them, so a nudge honors whatever the original
    run was configured with.
    """
    return RunRequest(
        prompt=_NUDGE_CONTINUATION_PROMPT,
        session_id=run.session_id,
        model=run.model,
        agent=run.agent,
    )
