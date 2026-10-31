"""The `AgentRunner.run()` data model: `RunRequest` in, a stream of `RunEvent`s
plus a terminal `RunOutcome` out. See issue #75.

Deliberately a thin, forward-compatible envelope rather than a typed mirror of
OpenCode's own `--format json` event vocabulary: the opencode.ai docs (checked
while planning this issue) describe that output only as "raw JSON events,
line-delimited," with no published event-type names or payload shapes. Per
this codebase's own "verify, don't assume" discipline (the Vector-0.45.x pin,
the v1-vs-v2 `--version` output shape), `parse_run_event` does not hard-code
guessed type-string literals. It extracts whatever string the parsed JSON
object carries under a `type` key (falling back to `event`) into `RunEvent.
raw_type`, maps it through `_RAW_TYPE_TO_EVENT_TYPE` — intentionally empty
today — and defaults anything unmapped to `RunEventType.OTHER`. `raw_type` is
always preserved verbatim (truncated+redacted, like `payload`) even when
`type` is `OTHER`, so no information is lost while the table is empty, and
populating it later (against a real captured `opencode run --format json`
sample) changes no downstream behavior for an event that was already
classified `OTHER` — it only ever makes a previously-`OTHER` event more
specific.

Mirrors `heart.tick.decision`'s own shape exactly — one module holding both
the enum and the pure, `None`-returning parser (the issue's own "Reuses"
section names that module explicitly): `parse_run_event` never logs, never
raises, and never defaults a class the way `heart.tick.decision.
parse_decision` does, because this module does not own the "how do we handle
a line that doesn't parse" policy — `ansina.opencode.runner.OpencodeCliRunner`
does, counting a `None` result as skipped rather than fatal.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from ansina.logging.redaction import redact


class RunEventType(StrEnum):
    """The normalized classification every `RunEvent` carries. `OTHER` is the
    mandatory forward-compat fallback (issue #75's own acceptance criterion:
    "An unrecognized event type normalizes to `other` and is still counted").

    No other member exists yet — see module docstring for why. `RunEvent.
    raw_type` carries the real, as-emitted string regardless of how `type`
    classifies, so nothing is lost while this enum stays this small.
    """

    OTHER = "other"


# Raw `type`/`event` string -> `RunEventType`. Intentionally empty: see module
# docstring. A caller that has captured a real `opencode run --format json`
# sample should add entries here (e.g. `"tool_call": RunEventType.TOOL_CALL`,
# adding that member alongside) rather than anywhere else — this is the one
# place classification is decided.
_RAW_TYPE_TO_EVENT_TYPE: Mapping[str, RunEventType] = {}


@dataclass(frozen=True, slots=True)
class RunEvent:
    """One classified, size-capped, redacted line from the run's event
    stream. `sequence` is the 0-based ordinal within this run, for ordering a
    downstream consumer (a future telemetry sink) can rely on without
    depending on arrival-order side channels.
    """

    type: RunEventType
    raw_type: str
    payload: str
    sequence: int


class RunStatus(StrEnum):
    """How a run concluded. `TIMEOUT` is deliberately distinct from `FAILED`
    (issue #75's own acceptance criterion) — a run that exceeded `[opencode]
    run_timeout_seconds` is a different condition than one whose child
    process exited non-zero or never spawned at all.
    """

    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """The terminal result `AgentRunner.run()` returns once the event stream
    ends — whether it ended because the child exited, because it was killed
    after `run_timeout_seconds`, or because `[opencode] max_run_output_bytes`
    was reached. `error` is a human-readable summary, set whenever `status`
    is not `COMPLETED`; `None` on a clean completion.
    """

    status: RunStatus
    exit_code: int | None
    events_emitted: int
    events_skipped: int
    duration_seconds: float
    truncated: bool = False
    error: str | None = None


@dataclass(frozen=True, slots=True)
class RunRequest:
    """Everything one `AgentRunner.run()` call needs. `model`/`agent`/
    `auto_approve` are per-call overrides of `[opencode] model`/`agent`/
    `auto_approve` — `None` (the default for each) means "use the configured
    value," not "omit the flag"; `ansina.opencode.runner.OpencodeCliRunner` is
    what resolves that fallback, not this dataclass.

    `session_id`/`continue_last`/`fork` mirror `opencode run`'s own
    `--session`/`--continue`/`--fork` flags one-for-one. The workspace
    directory is never part of a request — it is always `[opencode]
    workspace_dir`, resolved by the runner from `Settings`, never
    per-request-overridable (the whole point of the confinement issue #74
    built).
    """

    prompt: str
    session_id: str | None = None
    continue_last: bool = False
    fork: bool = False
    model: str | None = None
    agent: str | None = None
    auto_approve: bool | None = None

    def __post_init__(self) -> None:
        if self.session_id is not None and self.continue_last:
            raise ValueError(
                "RunRequest: session_id and continue_last are mutually "
                "exclusive — pass at most one"
            )
        if self.fork and self.session_id is None and not self.continue_last:
            raise ValueError(
                "RunRequest: fork=True requires session_id or continue_last "
                "— forking only makes sense when continuing a session"
            )


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncates `text` to at most `max_bytes` UTF-8 bytes, dropping a
    trailing multi-byte character split at the boundary rather than raising
    (`errors="ignore"`) — a plain `text[:max_bytes]` character slice would
    both over- and under-count for any non-ASCII content, which a prompt's
    own output (file contents, non-English text) can routinely carry.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def parse_run_event(
    line: str, *, max_event_bytes: int, sequence: int
) -> RunEvent | None:
    """One line of `opencode run --format json`'s stdout -> a `RunEvent`, or
    `None` if the line is blank or not a JSON object — the `try_parse_decision`
    seam (`heart.tick.decision`): pure, no logging, no default, leaving the
    "what does an unparseable line mean" policy entirely to the caller.

    Classification happens against the **original, untruncated** text — the
    only thing truncated+redacted is what ends up on `RunEvent.payload`/
    `raw_type`, matching issue #75's own acceptance criterion ("every captured
    event payload is truncated ... and run through redact() ... before it
    leaves the adapter"): truncating before parsing could cut a line
    mid-object and make every single line look malformed.
    """
    stripped = line.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None

    raw_type = parsed.get("type")
    if not isinstance(raw_type, str):
        raw_type = parsed.get("event")
    if not isinstance(raw_type, str):
        raw_type = ""

    event_type = _RAW_TYPE_TO_EVENT_TYPE.get(raw_type, RunEventType.OTHER)
    payload = redact(_truncate_utf8(stripped, max_event_bytes))
    redacted_raw_type = redact(_truncate_utf8(raw_type, max_event_bytes))
    return RunEvent(
        type=event_type,
        raw_type=redacted_raw_type,
        payload=payload,
        sequence=sequence,
    )
