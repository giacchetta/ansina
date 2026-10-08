from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ansina.auth.clock import iso
from ansina.heart.journal import BrainEscalationStatus, HeartJournalRepository
from ansina.heart.tick.decision import TickDecision

_NOW = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def _seed(app: FastAPI, *, count: int) -> None:
    repository = HeartJournalRepository(app.state.db)
    for minute in range(count):
        repository.append(
            created_at=iso(_NOW.replace(minute=minute)),
            tick_number=minute,
            decision=TickDecision.IDLE,
            note="",
            prompt_tokens=1,
            duration_seconds=0.1,
            max_entries=10_000,
            retention_days=3650,
        )


# --- deny-by-default: auth runs before route logic ----------------------------------


def test_get_journal_requires_auth_when_configured(authed_client: TestClient) -> None:
    response = authed_client.get("/heart/journal")

    assert response.status_code == 401


def test_read_role_can_list_the_journal(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("read")

    response = authed_client.get(
        "/heart/journal", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200


# --- no 503 branch: the journal is a table, not a live-loop handle ------------------


def test_get_journal_serves_historical_rows_with_the_heart_disabled(
    app: FastAPI, client: TestClient
) -> None:
    """Unlike `/heart/tick*`, this route needs only `app.state.db` — it must keep
    answering after the Heart is switched off, which is the whole point of a durable
    trace.
    """
    _seed(app, count=1)

    response = client.get("/heart/journal")

    assert response.status_code == 200
    assert response.json()["count"] == 1


# --- shape and pagination -------------------------------------------------------------


def test_get_journal_on_an_empty_table(client: TestClient) -> None:
    response = client.get("/heart/journal")

    assert response.status_code == 200
    body = response.json()
    assert body == {"entries": [], "count": 0, "limit": 50, "has_more": False}


def test_get_journal_returns_entries_newest_first(
    app: FastAPI, client: TestClient
) -> None:
    _seed(app, count=3)

    response = client.get("/heart/journal")

    body = response.json()
    assert [e["tick_number"] for e in body["entries"]] == [2, 1, 0]


def test_get_journal_entry_shape(app: FastAPI, client: TestClient) -> None:
    repository = HeartJournalRepository(app.state.db)
    repository.append(
        created_at=iso(_NOW),
        tick_number=5,
        decision=TickDecision.ACT,
        note="daemon_state reported: database unhealthy.",
        prompt_tokens=42,
        duration_seconds=1.5,
        max_entries=100,
        retention_days=30,
    )

    body = client.get("/heart/journal").json()

    entry = body["entries"][0]
    assert entry["tick_number"] == 5
    assert entry["decision"] == "act"
    assert entry["note"] == "daemon_state reported: database unhealthy."
    assert entry["prompt_tokens"] == 42
    assert entry["duration_seconds"] == 1.5
    assert entry["created_at"] == iso(_NOW)
    assert "id" in entry
    # Issue #64: no Brain interaction on this (non-escalate) row.
    assert entry["brain_status"] is None
    assert entry["brain_detail"] == ""
    assert entry["brain_prompt_tokens"] is None
    assert entry["brain_completion_tokens"] is None


def test_get_journal_entry_shape_with_a_brain_outcome(
    app: FastAPI, client: TestClient
) -> None:
    """Issue #64's four new fields, populated — the second row
    `BrainEscalationHandler` appends after an `escalate` tick that reached the Brain.
    """
    repository = HeartJournalRepository(app.state.db)
    repository.append(
        created_at=iso(_NOW),
        tick_number=5,
        decision=TickDecision.ESCALATE,
        note="",
        prompt_tokens=42,
        duration_seconds=0.9,
        max_entries=100,
        retention_days=30,
        brain_status=BrainEscalationStatus.CALLED,
        brain_detail="",
        brain_prompt_tokens=10,
        brain_completion_tokens=3,
    )

    entry = client.get("/heart/journal").json()["entries"][0]

    assert entry["brain_status"] == "called"
    assert entry["brain_prompt_tokens"] == 10
    assert entry["brain_completion_tokens"] == 3


def test_get_journal_respects_limit(app: FastAPI, client: TestClient) -> None:
    _seed(app, count=5)

    response = client.get("/heart/journal", params={"limit": 2})

    body = response.json()
    assert body["count"] == 2
    assert body["limit"] == 2
    assert body["has_more"] is True


def test_get_journal_has_more_is_false_when_exhausted(
    app: FastAPI, client: TestClient
) -> None:
    _seed(app, count=2)

    response = client.get("/heart/journal", params={"limit": 50})

    assert response.json()["has_more"] is False


def test_get_journal_filters_by_since(app: FastAPI, client: TestClient) -> None:
    _seed(app, count=3)  # ticks 0, 1, 2 at minutes 0, 1, 2

    response = client.get(
        "/heart/journal", params={"since": iso(_NOW.replace(minute=0))}
    )

    body = response.json()
    assert sorted(e["tick_number"] for e in body["entries"]) == [1, 2]


def test_get_journal_rejects_a_malformed_since(client: TestClient) -> None:
    response = client.get("/heart/journal", params={"since": "not-a-timestamp"})

    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "ansina.request.invalid"


def test_get_journal_rejects_limit_over_the_ceiling(client: TestClient) -> None:
    response = client.get("/heart/journal", params={"limit": 10_000})

    assert response.status_code == 422


def test_get_journal_rejects_limit_below_one(client: TestClient) -> None:
    response = client.get("/heart/journal", params={"limit": 0})

    assert response.status_code == 422
