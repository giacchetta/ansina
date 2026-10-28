"""`opencode_runs` — the mutable run-status table. See issue #77.

Opencode domain, not `auth/repositories.py`: this table has nothing to do with RBAC,
and nothing here is gated by a permission check (that's `api/routes/opencode_runs.py`'s
job). Mirrors `ansina.heart.journal.HeartJournalRepository`'s shape closely — same
`Database.connection()`/`Database.transaction()` foundation, same caller-supplied-clock
discipline, same "bounded retention swept as a side effect" pattern — but
**mutable**, not append-only: see `0012_opencode_runs.sql`'s own header comment for
why a run's lifecycle is tracked as "what is true right now" here, while the
append-only trace of that same lifecycle is the telemetry stream
(`ansina.opencode.telemetry.OpencodeTelemetryWriter`, issue #76), not this table.

`OpencodeRun.id` is a `uuid.uuid4().hex` string, the same convention
`auth/repositories.py`'s `_new_id()` and `HeartJournalRepository.append` already use.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Self

from ansina.auth.clock import iso
from ansina.storage.database import Database

# A run's status is never updated once it reaches one of these — enforced by
# `update_status()` below, not merely documented.
_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"succeeded", "failed", "timeout", "cancelled"}
)
_ACTIVE_STATUSES: tuple[str, ...] = ("queued", "running")


@dataclass(frozen=True, slots=True)
class OpencodeRun:
    """A row in `opencode_runs` — the current state of one submitted run."""

    id: str
    created_at: str
    started_at: str | None
    completed_at: str | None
    status: str
    session_id: str | None
    prompt: str
    model: str | None
    agent: str | None
    exit_code: int | None
    error_detail: str
    event_count: int
    tool_call_count: int
    permission_denied_count: int
    parse_error_count: int
    prompt_tokens: int | None
    completion_tokens: int | None
    duration_seconds: float | None
    retry_of: str | None
    intervention_count: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        return cls(
            id=row["id"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            status=row["status"],
            session_id=row["session_id"],
            prompt=row["prompt"],
            model=row["model"],
            agent=row["agent"],
            exit_code=row["exit_code"],
            error_detail=row["error_detail"],
            event_count=row["event_count"],
            tool_call_count=row["tool_call_count"],
            permission_denied_count=row["permission_denied_count"],
            parse_error_count=row["parse_error_count"],
            prompt_tokens=row["prompt_tokens"],
            completion_tokens=row["completion_tokens"],
            duration_seconds=row["duration_seconds"],
            retry_of=row["retry_of"],
            intervention_count=row["intervention_count"],
        )


class OpencodeRunRepository:
    """CRUD + the one status mutator on `opencode_runs`."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def create(
        self,
        *,
        created_at: str,
        prompt: str,
        session_id: str | None = None,
        model: str | None = None,
        agent: str | None = None,
        retry_of: str | None = None,
    ) -> OpencodeRun:
        """Inserts one `status='queued'` row. Never sweeps as a side effect, unlike
        `HeartJournalRepository.append()` — `OpencodeRunService.submit()` checks
        `count_active()` against `[opencode] queue_max_depth` itself *before* calling
        this, so a sweep here would only ever race against a service that has already
        made its own admission decision.
        """
        run_id = uuid.uuid4().hex
        with self._db.transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO opencode_runs
                    (id, created_at, status, session_id, prompt, model, agent,
                     error_detail, retry_of)
                VALUES (?, ?, 'queued', ?, ?, ?, ?, '', ?)
                """,
                (run_id, created_at, session_id, prompt, model, agent, retry_of),
            )
            row = cursor.execute(
                "SELECT * FROM opencode_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return OpencodeRun.from_row(row)

    def update_status(
        self,
        run_id: str,
        status: str,
        *,
        started_at: str | None = None,
        completed_at: str | None = None,
        exit_code: int | None = None,
        error_detail: str | None = None,
        event_count: int | None = None,
        tool_call_count: int | None = None,
        permission_denied_count: int | None = None,
        parse_error_count: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        duration_seconds: float | None = None,
        intervention_count: int | None = None,
    ) -> OpencodeRun:
        """The one mutator. Every keyword past `status` is optional and, when
        omitted (`None`), leaves that column untouched via `COALESCE(?, column)` —
        so a caller only ever states the fields it actually knows this transition
        changed (e.g. `update_status(id, "running", started_at=now)` touches nothing
        about `exit_code`/counters).

        Refuses a transition out of a terminal status (`succeeded`/`failed`/
        `timeout`/`cancelled`) — implemented as a single `UPDATE ... WHERE id = ?
        AND status NOT IN (...)` plus a rowcount check, never a read-then-write
        race. A zero rowcount here is an internal invariant violation, not a
        client-facing condition: no designed route path can ever call this twice
        with a terminal status already set (`OpencodeRunService._execute`'s own
        state machine calls this at most once per terminal outcome per run), so
        this asserts rather than raising a typed `AnsinaError` — the same
        "`assert principal is not None`" shape `api/authorization.py`'s `require()`
        already uses for a condition only a bug, never a caller, could trigger.
        """
        with self._db.transaction() as cursor:
            cursor.execute(
                f"""
                UPDATE opencode_runs
                   SET status = ?,
                       started_at = COALESCE(?, started_at),
                       completed_at = COALESCE(?, completed_at),
                       exit_code = COALESCE(?, exit_code),
                       error_detail = COALESCE(?, error_detail),
                       event_count = COALESCE(?, event_count),
                       tool_call_count = COALESCE(?, tool_call_count),
                       permission_denied_count =
                           COALESCE(?, permission_denied_count),
                       parse_error_count = COALESCE(?, parse_error_count),
                       prompt_tokens = COALESCE(?, prompt_tokens),
                       completion_tokens = COALESCE(?, completion_tokens),
                       duration_seconds = COALESCE(?, duration_seconds),
                       intervention_count = COALESCE(?, intervention_count)
                 WHERE id = ?
                   AND status NOT IN ({",".join("?" * len(_TERMINAL_STATUSES))})
                """,
                (
                    status,
                    started_at,
                    completed_at,
                    exit_code,
                    error_detail,
                    event_count,
                    tool_call_count,
                    permission_denied_count,
                    parse_error_count,
                    prompt_tokens,
                    completion_tokens,
                    duration_seconds,
                    intervention_count,
                    run_id,
                    *_TERMINAL_STATUSES,
                ),
            )
            assert cursor.rowcount == 1, (
                f"update_status({run_id!r}, {status!r}): no row updated — either "
                "the run does not exist, or its status is already terminal "
                "(a transition out of a terminal status is refused, never applied)"
            )
            row = cursor.execute(
                "SELECT * FROM opencode_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return OpencodeRun.from_row(row)

    def get(self, run_id: str) -> OpencodeRun | None:
        row = (
            self._db.connection()
            .execute("SELECT * FROM opencode_runs WHERE id = ?", (run_id,))
            .fetchone()
        )
        return None if row is None else OpencodeRun.from_row(row)

    def list_recent(self, *, limit: int, since: str | None = None) -> list[OpencodeRun]:
        """Newest-first, up to `limit` rows — byte-for-byte the
        `HeartJournalRepository.list_recent` shape (`since` filters to
        `created_at > since` by direct string comparison, no parse, relying on the
        same lexicographic-ISO-8601-sorts-as-text property every caller-supplied-clock
        table in this codebase does).
        """
        if since is None:
            rows = (
                self._db.connection()
                .execute(
                    "SELECT * FROM opencode_runs "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (limit,),
                )
                .fetchall()
            )
        else:
            rows = (
                self._db.connection()
                .execute(
                    "SELECT * FROM opencode_runs WHERE created_at > ? "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (since, limit),
                )
                .fetchall()
            )
        return [OpencodeRun.from_row(row) for row in rows]

    def count_active(self) -> int:
        """How many rows are currently `queued` or `running` — the service's own
        backlog check (`OpencodeRunService.submit()` against
        `[opencode] queue_max_depth`) and a later issue's active-run count.
        """
        (count,) = (
            self._db.connection()
            .execute(
                "SELECT COUNT(*) FROM opencode_runs WHERE status IN (?, ?)",
                _ACTIVE_STATUSES,
            )
            .fetchone()
        )
        return int(count)

    def sweep(self, now: datetime, *, max_entries: int, retention_days: int) -> int:
        """Deletes every row that is either older than `retention_days` *or* outside
        the newest `max_entries` — the same two-independent-bounds reasoning
        `HeartJournalRepository.sweep`/`LoginAttemptRepository.delete_expired`
        already document. Returns the number of rows deleted.
        """
        cutoff = iso(now - timedelta(days=retention_days))
        with self._db.transaction() as cursor:
            cursor.execute(
                """
                DELETE FROM opencode_runs
                 WHERE created_at < ?
                    OR id NOT IN (
                        SELECT id FROM opencode_runs
                        ORDER BY created_at DESC, id DESC
                        LIMIT ?
                    )
                """,
                (cutoff, max_entries),
            )
            return cursor.rowcount

    def sweep_stale_on_boot(self, now: datetime) -> int:
        """Crash recovery (issue #77's own AC): any row left `queued`/`running` from
        a previous process (the daemon crashed or was killed mid-run, never reaching
        a terminal status) is swept to `failed` with a fixed `error_detail` —
        otherwise a later issue's active-run count is permanently poisoned by one
        crash, since `count_active()` would count it forever. Called once from
        `create_app`'s lifespan, after `run_migrations`, before `OpencodeRunService`
        is ever handed a request. Returns the number of rows swept.
        """
        completed_at = iso(now)
        with self._db.transaction() as cursor:
            cursor.execute(
                """
                UPDATE opencode_runs
                   SET status = 'failed',
                       error_detail = 'daemon restarted',
                       completed_at = ?
                 WHERE status IN (?, ?)
                """,
                (completed_at, *_ACTIVE_STATUSES),
            )
            return cursor.rowcount
