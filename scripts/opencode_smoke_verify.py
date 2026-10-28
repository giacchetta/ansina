"""OpenCode submission smoke verifier (issue #77) — the happy-path check for
`scripts/opencode-smoke.sh`: submit one real run over HTTP against the already-
booted daemon, poll `GET /opencode/runs/{id}` to a terminal status, and verify
the spooled `kind=opencode/` telemetry file on disk carries the expected
lifecycle events for that run — never trusting the HTTP `202` alone.

Run via `scripts/opencode-smoke.sh`, never ad hoc: it needs the exact same
`ANSINA_*` environment the daemon it's talking to was booted with (in
particular `[server] host`/`port` and `[telemetry] spool_dir`), read through
`load_settings()` the same way every other Ansina entry point does — never a
second, parallel config path, the same discipline `scripts/dev_mode_verify.py`
already established.

Scope: happy path only, by design (confirmed with the project owner ahead of
implementation) — submit, poll to completion, verify the telemetry line
landed, the same narrow scope `dev_mode_verify.py` itself has. Cancel/intake-
pause each get their own exercise in the unit and e2e suites
(`tests/unit/opencode/test_service.py`, `tests/e2e/test_server.py`, both
against a fake `AgentRunner`); this script's one job is the thing neither of
those can prove — a REAL `opencode` binary, installed on this machine via
`make opencode-install`, actually runs a submitted prompt end to end.

Not `pytest`-collected or `mypy`-checked (same as every other `scripts/*.py`
tool — see `scripts/dev_mode_verify.py`), but `ruff check`/`ruff format`-clean.
"""

from __future__ import annotations

import json
import sys
import time

import httpx

from ansina.config import load_settings
from ansina.errors import AnsinaError

_POLL_TIMEOUT_SECONDS = 60.0
_POLL_INTERVAL_SECONDS = 0.5
_ACTIVE_STATUSES = ("queued", "running")
_TERMINAL_EVENTS = ("completed", "failed", "timeout", "cancelled")
_PROMPT = "Say hello in exactly three words."


def main() -> int:
    try:
        settings = load_settings()
    except AnsinaError as exc:
        print(f"FAIL: could not load settings: {exc}", file=sys.stderr)
        return 1

    base_url = f"http://{settings.server.host}:{settings.server.port}"

    status = httpx.get(f"{base_url}/opencode/status", timeout=5.0)
    if status.status_code != 200:
        print(
            f"FAIL: GET /opencode/status returned {status.status_code}: {status.text}",
            file=sys.stderr,
        )
        return 1
    if not status.json()["enabled"]:
        print(
            "FAIL: opencode is disabled — check [opencode] enabled and the "
            "daemon's own log for the preflight skip reason",
            file=sys.stderr,
        )
        return 1

    submit = httpx.post(
        f"{base_url}/opencode/runs", json={"prompt": _PROMPT}, timeout=10.0
    )
    if submit.status_code != 202:
        print(
            f"FAIL: POST /opencode/runs returned {submit.status_code}: {submit.text}",
            file=sys.stderr,
        )
        return 1
    run_id = submit.json()["id"]
    print(f"Submitted run {run_id!r}", file=sys.stderr)

    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
    run_status = "queued"
    while time.monotonic() < deadline:
        page = httpx.get(f"{base_url}/opencode/runs/{run_id}", timeout=5.0)
        run_status = page.json()["status"]
        if run_status not in _ACTIVE_STATUSES:
            break
        time.sleep(_POLL_INTERVAL_SECONDS)
    else:
        print(
            f"FAIL: run {run_id!r} never reached a terminal status within "
            f"{_POLL_TIMEOUT_SECONDS}s (last: {run_status!r})",
            file=sys.stderr,
        )
        return 1
    print(f"Run {run_id!r} reached status={run_status!r}", file=sys.stderr)

    log_path = settings.telemetry.spool_dir / "opencode.jsonl"
    if not log_path.exists():
        print(f"FAIL: no telemetry file at {log_path}", file=sys.stderr)
        return 1
    lines = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    own_events = [line["event"] for line in lines if line.get("run_id") == run_id]
    if not own_events or own_events[0] != "submitted":
        print(
            f"FAIL: expected a 'submitted' telemetry line first for "
            f"{run_id!r}, got {own_events!r}",
            file=sys.stderr,
        )
        return 1
    if own_events[-1] not in _TERMINAL_EVENTS:
        print(
            f"FAIL: expected a terminal telemetry event last for {run_id!r}, "
            f"got {own_events!r}",
            file=sys.stderr,
        )
        return 1

    print(f"PASS: run {run_id!r} -> status={run_status!r}, events={own_events!r}")
    if run_status != "succeeded":
        print(
            f"NOTE: the run itself did not succeed (status={run_status!r}) — "
            f"check run.log and GET /opencode/runs/{run_id}'s own error_detail",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
