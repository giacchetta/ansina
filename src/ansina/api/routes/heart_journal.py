"""`GET /heart/journal` — a read-only window onto `heart_journal`. See issue #55.

A sibling module to `api/routes/heart.py`, not an addition to it: that module is about
the tick-loop *controller* (`heart.tick`, all three routes sharing one resource with a
503-when-disabled branch for "no `TickLoop` exists"). The journal shares neither — it
is a plain table (`heart.journal.HeartJournalRepository`), needs only `app.state.db`,
and keeps serving historical rows after the Heart is switched off, which is the whole
point of a durable trace. So: no 503 branch here, and a resource of its own
(`heart.journal`) rather than reusing `heart.tick`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio.to_thread
from fastapi import APIRouter, Query, Request
from fastapi.params import Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ansina.api.authorization import require
from ansina.api.problems import CODE_REQUEST_INVALID, problem_response
from ansina.auth.clock import parse_iso
from ansina.heart.journal import HeartJournalRepository, JournalEntry
from ansina.heart.tick.decision import TickDecision

if TYPE_CHECKING:
    from ansina.storage.database import Database

router = APIRouter(prefix="/heart")

_RESOURCE = "heart.journal"
_DESCRIPTION = "A durable, read-only trace of what every completed tick decided."

# `Query(..., le=...)` ceiling: an unbounded `limit` would let one request force a
# full-table scan — the same "never trust a caller-supplied page size" reasoning any
# paginated route needs, even though nothing else in this codebase has shipped one yet.
_MAX_LIMIT = 500
_DEFAULT_LIMIT = 50


def _require_journal() -> Depends:
    """A fresh `require(...)` dependency, mirroring `routes/heart.py`'s own
    `_require_tick()` — one call site per route, never a shared `Depends` instance.
    """
    return Depends(require(_RESOURCE, description=_DESCRIPTION))


class JournalEntryOut(BaseModel):
    id: str
    created_at: str
    tick_number: int
    decision: TickDecision
    note: str
    prompt_tokens: int
    duration_seconds: float

    @classmethod
    def from_model(cls, entry: JournalEntry) -> JournalEntryOut:
        return cls(
            id=entry.id,
            created_at=entry.created_at,
            tick_number=entry.tick_number,
            decision=entry.decision,
            note=entry.note,
            prompt_tokens=entry.prompt_tokens,
            duration_seconds=entry.duration_seconds,
        )


class JournalPage(BaseModel):
    """Newest-first. `has_more` is computed by querying `limit + 1` rows — an honest
    pagination contract that composes with newest-first ordering, unlike a
    `next_since` cursor (which would have to be the *oldest* returned row's
    `created_at`, awkward to reason about against a live, still-growing table).
    """

    entries: list[JournalEntryOut]
    count: int
    limit: int
    has_more: bool


def _list_journal(db: Database, limit: int, since: str | None) -> JournalPage:
    repository = HeartJournalRepository(db)
    # `limit + 1`: an extra row beyond what's returned tells us whether there's more
    # to page through, without a second round-trip.
    entries = repository.list_recent(limit=limit + 1, since=since)
    has_more = len(entries) > limit
    page = entries[:limit]
    return JournalPage(
        entries=[JournalEntryOut.from_model(entry) for entry in page],
        count=len(page),
        limit=limit,
        has_more=has_more,
    )


def _invalid_since_response(since: str) -> JSONResponse:
    return problem_response(
        status=400,
        code=CODE_REQUEST_INVALID,
        title="Invalid Query Parameter",
        detail=f"'since' is not a valid ISO 8601 timestamp: {since!r}",
    )


@router.get(
    "/journal",
    response_model=None,
    responses={
        200: {"model": JournalPage},
        400: {"description": "'since' is not a valid ISO 8601 timestamp."},
    },
    dependencies=[_require_journal()],
)
async def get_journal(
    request: Request,
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    since: str | None = Query(default=None),
) -> JournalPage | JSONResponse:
    if since is not None:
        try:
            parse_iso(since)
        except ValueError:
            return _invalid_since_response(since)

    return await anyio.to_thread.run_sync(
        _list_journal, request.app.state.db, limit, since
    )
