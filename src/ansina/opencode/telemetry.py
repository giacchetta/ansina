"""`OpencodeTelemetryWriter` — the event-driven producer for a run's own
lifecycle trace. See issue #76.

The third telemetry *shape* this codebase ships, alongside issue #61's interval
sampler (`ansina.telemetry.sampler.TelemetrySampler`) and log mirror
(`ansina.telemetry.log_mirror.TelemetryLogHandler`): one JSON line per
run-lifecycle event (`submitted`, `started`, each in-run event, a terminal
`completed`/`failed`/`timeout`/`cancelled`, and a reserved `intervention` event
populated by a later issue — no code here emits it yet, the same "leave the
table empty until a real caller needs it" discipline `ansina.opencode.events`'
own `_RAW_TYPE_TO_EVENT_TYPE` already follows) through the exact same generic
`ansina.telemetry.rotation.SpooledRotatingWriter` the other two families reuse,
under its own `file_prefix="opencode"` family.

**This issue is positioned deliberately before the run-submission API**: per the
project owner's own requirement, every OpenCode interaction, issue, and failure
must be telemetried before a run can ever be submitted over HTTP. Like issues
#74/#75, this module ships fully unit-tested and **wired into nothing** —
`build_opencode_telemetry_writer` is never called from `create_app`/
`__main__.py`; a later issue's run-submission route/executor loop is what
actually constructs one and drives its methods.

A real gap this module closes as a side effect: `ansina.opencode.events.
parse_run_event` redacts the *output* event stream (`RunEvent.payload`/
`raw_type`) but nothing anywhere redacts `RunRequest.prompt` — arbitrary user
input that could carry a pasted secret. `record_submitted` below is the first
code path that ever puts a prompt through `ansina.logging.redaction.redact`.

**No `ansina.heart` import anywhere in this module's own dependency chain**
(`ansina.telemetry.rotation`, `ansina.opencode.events`, `ansina.logging.
redaction`, stdlib only) — unlike `ansina.telemetry.sampler`, which needs
`ansina.heart.tick.loop.TickController` and is therefore deliberately kept out
of `ansina.telemetry.__init__`'s own re-exports (see that package's module
docstring), this module carries no such risk and is eagerly re-exported from
`ansina.opencode.__init__` with no cycle.

**No `ansina.storage`/`Database` dependency at all** — the mutable run row (a
later issue's own concern) is a separate thing from this append-only trace;
this writer never opens a database connection and never needs one.

`self._seq_by_run` tracks one monotonic counter per in-flight `run_id`,
incremented on every line written for that run and popped the moment a
terminal event (`record_outcome`/`record_cancelled`) is written — so the map
never grows unbounded across the daemon's whole lifetime, no matter how many
runs it has driven. A consumer of the resulting corpus can detect a dropped
line for a given `run_id` by checking for a gap in its own `seq` values (see
`docs/ml/corpus-contract.md`'s own gap-detection note for this family).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from ansina.config.settings import Settings
from ansina.logging import get_logger
from ansina.logging.redaction import redact
from ansina.opencode.events import RunEvent, RunOutcome, RunRequest
from ansina.telemetry.rotation import SpooledRotatingWriter

logger = get_logger(__name__)

Clock = Callable[[], float]

# The file-prefix family this writer's own `SpooledRotatingWriter` owns — a
# module constant, not a setting (the issue's own "Config keys added: None"
# line), mirroring `"samples"`/`"log"` in `ansina.telemetry.sampler`/
# `ansina.telemetry.log_mirror`.
_FILE_PREFIX = "opencode"

# A forward-compatible, best-effort passthrough: if a `RunEvent.payload`
# happens to parse as a JSON object carrying any of these keys, they're
# promoted to top-level fields on the written line (exactly the AC's own named
# examples — a tool call's `tool`/`command`/`path`, a permission denial's
# `permission`/`pattern`/`decision`, a usage report's `prompt_tokens`/
# `completion_tokens`/`cost`). This is deliberately *not* a classification
# decision keyed off `RunEvent.type` (which is always `RunEventType.OTHER`
# today, per `ansina.opencode.events`' own intentionally-empty lookup table) —
# just a flat, no-policy field passthrough. `payload` itself is always kept
# too, so nothing is ever lost because a key wasn't on this list yet.
_KNOWN_EVENT_FIELDS: tuple[str, ...] = (
    "tool",
    "command",
    "path",
    "permission",
    "pattern",
    "decision",
    "prompt_tokens",
    "completion_tokens",
    "cost",
)

# A terminal event pops the run's own seq counter — see module docstring.
_TERMINAL_EVENTS = frozenset({"completed", "failed", "timeout", "cancelled"})

# How much of `RunRequest.prompt` to preview in a `submitted` line, as a
# fraction of the writer's own `max_line_bytes` cap — generous enough to be
# useful, small enough that `_write`'s own cap re-assertion is a rare
# fallback rather than the normal path for every submitted run.
_PROMPT_PREVIEW_FRACTION = 4


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncates `text` to at most `max_bytes` UTF-8 bytes, dropping a
    trailing multi-byte character split at the boundary rather than raising
    (`errors="ignore"`) — the identical helper `ansina.opencode.events.
    parse_run_event` already uses for the same reason, duplicated locally
    (both modules are small and runtime, not a dev-tooling/runtime split
    worth threading a shared import through) rather than importing a
    module-private name across files.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _extract_known_fields(payload: str) -> dict[str, Any]:
    """Best-effort: if `payload` parses as a JSON object, returns whichever of
    `_KNOWN_EVENT_FIELDS` it carries. Returns `{}` on anything else (not valid
    JSON — `parse_run_event`'s own truncation can cut a line mid-object — or
    not an object at all) rather than raising; this is a convenience
    flattening for a query engine, never a required classification.
    """
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {key: parsed[key] for key in _KNOWN_EVENT_FIELDS if key in parsed}


class OpencodeTelemetryWriter:
    """Pure sink: every `record_*` method below builds one JSON line and
    appends it via `SpooledRotatingWriter.write_line`, inside its own
    swallow-and-log boundary — a write never raises into the caller, the same
    "fault isolation at the boundary" contract `SpooledRotatingWriter.
    _enforce_budget`'s own docstring already names, applied here the way
    `ansina.telemetry.log_mirror.TelemetryLogHandler.emit`/`ansina.telemetry.
    sampler.TelemetrySampler._run` each apply it at their own call site.

    Flat methods, not a per-run handle object: `run_id`/`session_id` are
    passed to every call rather than captured by a returned object, keeping
    this class's own surface a single, simple "pure sink" shape. A future
    caller that wants an `ansina.opencode.runner.OnEvent`-compatible callback
    binds `run_id`/`session_id` itself via `functools.partial(writer.
    record_event, run_id, session_id)`.
    """

    def __init__(
        self,
        *,
        writer: SpooledRotatingWriter,
        max_line_bytes: int,
        clock: Clock = time.time,
    ) -> None:
        self._writer = writer
        self._max_line_bytes = max_line_bytes
        self._clock = clock
        self._seq_by_run: dict[str, int] = {}

    def record_submitted(
        self, run_id: str, session_id: str | None, request: RunRequest
    ) -> None:
        """`event: "submitted"` — the first line for a run, before any
        subprocess exists. `request.prompt` is previewed (truncated here,
        redacted uniformly by `_write` below) since it is arbitrary,
        unbounded user input that has never passed through `ansina.logging.
        redaction.redact` anywhere else in this codebase.
        """
        preview = _truncate_utf8(
            request.prompt, self._max_line_bytes // _PROMPT_PREVIEW_FRACTION
        )
        self._write(
            run_id,
            session_id,
            "submitted",
            {
                "prompt": preview,
                "continue_last": request.continue_last,
                "fork": request.fork,
                "model": request.model,
                "agent": request.agent,
                "auto_approve": request.auto_approve,
            },
        )

    def record_started(self, run_id: str, session_id: str | None) -> None:
        """`event: "started"` — the subprocess has actually been spawned (or
        its spawn attempted, per `ansina.opencode.runner`'s own admission-
        control framing). No fields.
        """
        self._write(run_id, session_id, "started", {})

    def record_event(
        self, run_id: str, session_id: str | None, event: RunEvent
    ) -> None:
        """`event: "run_event"` — one line per classified line from the run's
        own stdout stream. Signature-compatible with `ansina.opencode.
        runner.OnEvent` (`Callable[[RunEvent], None]`) once `run_id`/
        `session_id` are bound by the caller — see class docstring.
        """
        fields: dict[str, Any] = {
            "raw_type": event.raw_type,
            "payload": event.payload,
        }
        fields.update(_extract_known_fields(event.payload))
        self._write(run_id, session_id, "run_event", fields)

    def record_outcome(
        self, run_id: str, session_id: str | None, outcome: RunOutcome
    ) -> None:
        """`event: outcome.status.value` — one of `"completed"`/`"failed"`/
        `"timeout"`, directly off `RunStatus`. Terminal: pops the run's own
        seq counter (see module docstring).
        """
        self._write(
            run_id,
            session_id,
            outcome.status.value,
            {
                "exit_code": outcome.exit_code,
                "duration_seconds": outcome.duration_seconds,
                "events_emitted": outcome.events_emitted,
                "events_skipped": outcome.events_skipped,
                "truncated": outcome.truncated,
                "error": outcome.error,
            },
        )

    def record_cancelled(
        self, run_id: str, session_id: str | None, *, duration_seconds: float
    ) -> None:
        """`event: "cancelled"` — the one outcome `RunOutcome` cannot itself
        represent: `ansina.opencode.runner.OpencodeCliRunner.run()` lets
        `asyncio.CancelledError` propagate rather than resolving it into a
        `RunOutcome` (see that module's own docstring), so a future caller
        that catches the cancellation reports it here explicitly. Terminal.
        """
        self._write(
            run_id, session_id, "cancelled", {"duration_seconds": duration_seconds}
        )

    def _next_seq(self, run_id: str, *, terminal: bool) -> int:
        seq = self._seq_by_run.get(run_id, 0)
        if terminal:
            self._seq_by_run.pop(run_id, None)
        else:
            self._seq_by_run[run_id] = seq + 1
        return seq

    def _write(
        self,
        run_id: str,
        session_id: str | None,
        event: str,
        fields: Mapping[str, Any],
    ) -> None:
        try:
            seq = self._next_seq(run_id, terminal=event in _TERMINAL_EVENTS)
            line: dict[str, Any] = {
                "t": self._clock(),
                "run_id": run_id,
                "session_id": session_id,
                "seq": seq,
                "event": event,
                **fields,
            }
            serialized = redact(json.dumps(line, separators=(",", ":"), default=str))
            if len(serialized.encode("utf-8")) > self._max_line_bytes:
                serialized = json.dumps(
                    {
                        "t": line["t"],
                        "run_id": run_id,
                        "session_id": session_id,
                        "seq": seq,
                        "event": event,
                        "truncated": True,
                    },
                    separators=(",", ":"),
                )
            self._writer.write_line(serialized)
        except Exception:
            logger.exception(
                "opencode telemetry: failed to write line",
                extra={"run_id": run_id, "event": event},
            )


def build_opencode_telemetry_writer(
    settings: Settings,
) -> OpencodeTelemetryWriter | None:
    """The `None`-when-off factory, mirroring `ansina.opencode.preflight.
    build_opencode_service`'s own gate: `None` when `[opencode] enabled` is
    `false`, not merely when the generic `[telemetry] enabled` is — this
    writer exists specifically to telemetry OpenCode runs, so gating on the
    narrower flag is correct (and `Settings._refuse_unobserved_executor`
    already guarantees `opencode.enabled=True` implies `telemetry.enabled=
    True`, so this is never looser in practice than gating on the generic
    flag would be).

    Budget (`spool_dir`/`max_file_bytes`/`max_spool_bytes`/`retention_hours`)
    comes from `[telemetry]`, the same shared pool `ansina.telemetry.sampler.
    build_telemetry_sampler` draws from for its own family; `max_line_bytes`
    reuses `[opencode] max_event_bytes` — no new config key for either, per
    the issue's own "Config keys added: None" line.

    **Not called from `create_app`/`__main__.py`** — see module docstring.
    """
    if not settings.opencode.enabled:
        return None
    telemetry = settings.telemetry
    writer = SpooledRotatingWriter(
        spool_dir=telemetry.spool_dir,
        file_prefix=_FILE_PREFIX,
        max_file_bytes=telemetry.max_file_bytes,
        max_spool_bytes=telemetry.max_spool_bytes,
        retention_hours=telemetry.retention_hours,
    )
    return OpencodeTelemetryWriter(
        writer=writer, max_line_bytes=settings.opencode.max_event_bytes
    )
