"""`heart_journal` — a durable, readable trace of what every completed tick decided.
See issue #55.

Heart domain, not `auth/repositories.py`: this table has nothing to do with RBAC, and
nothing here is gated by a permission check (that's `api/routes/heart_journal.py`'s
job). Mirrors `auth.repositories.LoginAttemptRepository`'s shape closely — same
`Database.connection()`/`Database.transaction()` foundation, same caller-supplied-clock
discipline, same "bounded retention swept as a side effect of the write path" pattern
`LoginThrottle.record_failure` already established for `login_attempts`.

`JournalEntry.id` is a `uuid.uuid4().hex` string, the same convention
`auth/repositories.py`'s `_new_id()` already uses for every other table in this
codebase.

`TickDecision` (`heart.tick.decision`) is only ever imported here under
`TYPE_CHECKING`, with one deferred, function-scoped import in `JournalEntry.from_row`
for its single real runtime use — never at module scope. `heart.tick`'s own package
`__init__` imports this module (via `journal_handler.py`/`loop.py`/
`sources/recent_journal.py`), so a module-scope `from ansina.heart.tick.decision
import TickDecision` here would make `ansina.heart.journal` and `ansina.heart.tick`
need each other fully initialized before either can finish — which Python's import
system can't satisfy regardless of which one a caller touches first, since
`ansina.heart.tick.decision` (a submodule) can't be reached without first running
`ansina.heart.tick`'s own `__init__.py`. Deferring the one real runtime need avoids
that entirely: by the time `from_row()` is ever actually called, the whole package
graph has long finished importing.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Self

from ansina.auth.clock import iso, parse_iso
from ansina.storage.database import Database

if TYPE_CHECKING:
    from ansina.heart.tick.decision import TickDecision


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """A row in `heart_journal` — one completed tick's decision and (for `act`/
    `escalate`) the code-generated note explaining what triggered it.
    """

    id: str
    created_at: str
    tick_number: int
    decision: TickDecision
    note: str
    prompt_tokens: int
    duration_seconds: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        # Deferred import — see this module's own docstring for why `TickDecision`
        # is never imported at module scope here.
        from ansina.heart.tick.decision import TickDecision

        return cls(
            id=row["id"],
            created_at=row["created_at"],
            tick_number=row["tick_number"],
            decision=TickDecision(row["decision"]),
            note=row["note"],
            prompt_tokens=row["prompt_tokens"],
            duration_seconds=row["duration_seconds"],
        )


class HeartJournalRepository:
    """CRUD on `heart_journal`. Policy-free, like `CredentialRepository.set_password`:
    retention *numbers* (`max_entries`/`retention_days`) are always supplied by the
    caller (`heart.tick.journal_handler.JournalDecisionHandler`, reading
    `HeartSettings.journal`), never read from config here.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def append(
        self,
        *,
        created_at: str,
        tick_number: int,
        decision: TickDecision,
        note: str,
        prompt_tokens: int,
        duration_seconds: float,
        max_entries: int,
        retention_days: int,
    ) -> JournalEntry:
        """Inserts one row, then sweeps bounded retention as a documented side
        effect — the same "sweep on the already-writing path" shape
        `LoginThrottle.record_failure` uses for `login_attempts`.
        """
        entry_id = uuid.uuid4().hex
        with self._db.transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO heart_journal
                    (id, created_at, tick_number, decision, note, prompt_tokens,
                     duration_seconds)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    created_at,
                    tick_number,
                    decision.value,
                    note,
                    prompt_tokens,
                    duration_seconds,
                ),
            )
            row = cursor.execute(
                "SELECT * FROM heart_journal WHERE id = ?", (entry_id,)
            ).fetchone()
        entry = JournalEntry.from_row(row)
        self.sweep(
            parse_iso(created_at),
            max_entries=max_entries,
            retention_days=retention_days,
        )
        return entry

    def list_recent(
        self, *, limit: int, since: str | None = None
    ) -> list[JournalEntry]:
        """Newest-first, up to `limit` rows. `since` (an ISO 8601 string) filters to
        `created_at > since` by direct string comparison, no parse — the same
        lexicographic-ISO-8601-sorts-as-text property `SudoGrantRepository.find_active`
        already relies on for `expires_at > ?`.
        """
        if since is None:
            rows = (
                self._db.connection()
                .execute(
                    "SELECT * FROM heart_journal "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (limit,),
                )
                .fetchall()
            )
        else:
            rows = (
                self._db.connection()
                .execute(
                    "SELECT * FROM heart_journal WHERE created_at > ? "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (since, limit),
                )
                .fetchall()
            )
        return [JournalEntry.from_row(row) for row in rows]

    def sweep(self, now: datetime, *, max_entries: int, retention_days: int) -> int:
        """Deletes every row that is either older than `retention_days` *or* outside
        the newest `max_entries` — two independent bounds, not one, the same
        reasoning `LoginAttemptRepository.delete_expired`'s two separate cutoffs
        document: nothing guarantees one bound is tighter than the other, so a row
        failing either test must go. Returns the number of rows deleted.
        """
        cutoff = iso(now - timedelta(days=retention_days))
        with self._db.transaction() as cursor:
            cursor.execute(
                """
                DELETE FROM heart_journal
                 WHERE created_at < ?
                    OR id NOT IN (
                        SELECT id FROM heart_journal
                        ORDER BY created_at DESC, id DESC
                        LIMIT ?
                    )
                """,
                (cutoff, max_entries),
            )
            return cursor.rowcount
