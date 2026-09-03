"""Black-box: launches `python -m ansina` as a real subprocess and talks HTTP only.

Never imports `ansina.api` internals (blueprint §5) — this is the gate that answers
"does a fresh build actually work," independent of whether the unit tests pass.
"""

from __future__ import annotations

import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

# The one exception to this module's "never import `ansina.api` internals" rule
# (blueprint §5): `ansina.auth.hashing` is the domain-layer credential-hashing
# scheme, not an API-layer internal — reusing it here to seed a token directly into
# the running server's SQLite file means this test never re-implements salted-SHA256
# by hand, and fails loudly (an import error, not a silently-wrong hash) if that
# scheme ever changes shape.
from ansina.auth.hashing import Argon2Params, hash_password, hash_token, new_token_salt

_STARTUP_TIMEOUT_S = 15.0
_POLL_INTERVAL_S = 0.1

# Long enough and high-entropy enough to clear `SecuritySettings.api_token`'s
# strength bar (>=32 chars, base64url charset, >=2.5 bits/char) — see
# `config/settings.py`'s `_TOKEN_MIN_LENGTH`/`_TOKEN_CHARSET`/
# `_TOKEN_MIN_ENTROPY_BITS_PER_CHAR`.
_E2E_TOKEN = "e2e-test-token-0123456789abcdefghij"
_E2E_READ_TOKEN = "e2e-read-role-token-0123456789abcd"
_E2E_MAINTAIN_TOKEN = "e2e-maintain-role-token-0123456789ab"
_E2E_MAINTAIN_PASSWORD = "correct horse battery staple e2e"
# Cheap, test-only argon2id work factors (matches `tests/unit/auth/conftest.py`'s
# `cheap_argon2` fixture) — the running server's own configured params never matter
# for *verifying* this hash: argon2-cffi parses them back out of the PHC-format
# string itself (see `ansina.auth.hashing`'s module docstring).
_E2E_CHEAP_ARGON2 = Argon2Params(time_cost=1, memory_cost_kib=8, parallelism=1)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@dataclass
class Server:
    """A launched `python -m ansina` subprocess, still healthy when yielded.

    Carries the raw `Popen` (not just the base URL) so a test can inspect the exit
    code and captured output after shutdown — e.g. to prove a clean SIGTERM shutdown,
    not just that the process eventually dies.
    """

    base_url: str
    process: subprocess.Popen[str]


@contextmanager
def _launch_server(
    tmp_path: Path, *, env: dict[str, str] | None = None
) -> Iterator[Server]:
    """Launch `python -m ansina` against `tmp_path`'s config, yielding its `Server`.

    A `@contextmanager` rather than a bare generator so a test can start, stop, and
    restart the server against the *same* `tmp_path` — e.g. to prove a migration
    applied on the first boot is not re-applied on the second.
    """
    port = _free_port()
    (tmp_path / "ansina.toml").write_text(
        f'[server]\nhost = "127.0.0.1"\nport = {port}\n'
        f'[database]\npath = "{(tmp_path / "ansina.db").as_posix()}"\n',
        encoding="utf-8",
    )
    base_url = f"http://127.0.0.1:{port}"

    process = subprocess.Popen(  # fixed argv, no shell, no untrusted input
        [sys.executable, "-m", "ansina"],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, **(env or {})},
    )
    try:
        deadline = time.monotonic() + _STARTUP_TIMEOUT_S
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                pytest.fail(
                    f"ansina exited early (code {process.returncode}):\n{output}"
                )
            try:
                response = httpx.get(f"{base_url}/healthz", timeout=1.0)
                if response.status_code == 200:
                    break
            except httpx.HTTPError as exc:
                last_error = exc
            time.sleep(_POLL_INTERVAL_S)
        else:
            process.kill()
            raise TimeoutError(f"ansina never became healthy: {last_error}")

        yield Server(base_url=base_url, process=process)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.fixture
def server(tmp_path: Path) -> Iterator[str]:
    """`ANSINA_SECURITY__ENABLED=false` — auth disabled, every route reachable.

    Since issue #24, an *unset* `ANSINA_SECURITY__API_TOKEN` no longer implies "no
    auth" — `security.enabled` defaults to `true` and Ansina would instead
    auto-generate and enforce its own bootstrap token — so dev mode has to be
    requested explicitly here.
    """
    with _launch_server(tmp_path, env={"ANSINA_SECURITY__ENABLED": "false"}) as srv:
        yield srv.base_url


@pytest.fixture
def authed_server(tmp_path: Path) -> Iterator[str]:
    """ANSINA_SECURITY__API_TOKEN set in the child process — auth enforced via that
    operator-supplied override, not the auto-generated path (see
    `test_bootstrap_token_is_generated_printed_once_and_authenticates` for that one).
    """
    with _launch_server(
        tmp_path, env={"ANSINA_SECURITY__API_TOKEN": _E2E_TOKEN}
    ) as srv:
        yield srv.base_url


def test_healthz(server: str) -> None:
    response = httpx.get(f"{server}/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz(server: str) -> None:
    response = httpx.get(f"{server}/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] is True


def test_readyz_has_no_heart_key_when_heart_is_disabled(server: str) -> None:
    """`[heart] enabled` defaults to `false` (issue #10) — the default boot must be
    entirely unaffected: no `heart` readiness key at all, not even a `false` one.
    """
    response = httpx.get(f"{server}/readyz")

    assert "heart" not in response.json()["checks"]


def test_version(server: str) -> None:
    response = httpx.get(f"{server}/version")

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "ansina"
    assert body["version"]


def test_openapi_schema(server: str) -> None:
    response = httpx.get(f"{server}/openapi.json")

    assert response.status_code == 200
    assert set(response.json()["paths"]) == {
        "/healthz",
        "/readyz",
        "/version",
        "/heart/tick",
        "/heart/tick/pause",
        "/heart/tick/resume",
        "/auth/sudo",
        "/auth/sudo/grants",
    }


def test_docs_and_redoc_are_gone(server: str) -> None:
    """Issue #25: `create_app` disables FastAPI's default `/docs`/`/redoc`/`/docs/
    oauth2-redirect` — plain Starlette routes that can't carry a `require(...)`
    authorization declaration, and non-functional with auth enabled regardless (no
    `fastapi.security` scheme is declared for Swagger UI's "Authorize" button to use).
    `/openapi.json` (`test_openapi_schema`) is the one FastAPI default kept, re-served
    as a gated route of our own.
    """
    assert httpx.get(f"{server}/docs").status_code == 404
    assert httpx.get(f"{server}/redoc").status_code == 404
    assert httpx.get(f"{server}/docs/oauth2-redirect").status_code == 404


def test_request_id_is_echoed(server: str) -> None:
    response = httpx.get(f"{server}/healthz", headers={"X-Request-ID": "e2e-trace"})

    assert response.headers["x-request-id"] == "e2e-trace"


def test_unknown_path_is_problem_json(server: str) -> None:
    response = httpx.get(f"{server}/nope")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "ansina.not_found"


def test_heart_tick_503_when_heart_disabled(server: str) -> None:
    """`[heart] enabled` defaults to `false` (issue #10) — with no Heart there is no
    tick loop (issue #11) either, so every `/heart/tick*` route must answer 503
    `problem+json` rather than pretending a loop exists.
    """
    response = httpx.get(f"{server}/heart/tick")

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "ansina.heart.disabled"


def test_authed_heart_tick_requires_token(authed_server: str) -> None:
    response = httpx.get(f"{authed_server}/heart/tick")

    assert response.status_code == 401
    assert response.json()["code"] == "ansina.unauthorized"


def test_authed_healthz_reachable_without_token(authed_server: str) -> None:
    response = httpx.get(f"{authed_server}/healthz")

    assert response.status_code == 200


def test_authed_protected_route_rejects_missing_token(authed_server: str) -> None:
    response = httpx.get(f"{authed_server}/version")

    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "ansina.unauthorized"


def test_authed_protected_route_accepts_valid_token(authed_server: str) -> None:
    response = httpx.get(
        f"{authed_server}/version",
        headers={"Authorization": f"Bearer {_E2E_TOKEN}"},
    )

    assert response.status_code == 200
    assert response.json()["name"] == "ansina"


def test_read_role_token_progresses_401_then_403_then_200(
    authed_server: str, tmp_path: Path
) -> None:
    """Issue #25's acceptance criterion, black-box end to end: no token is 401, a
    `Read`-role token gets 403 on a mutating route it holds no grant for, and 200 on a
    `GET` it does. `tmp_path` is the same directory `authed_server`'s own fixture
    already launched the server against (pytest caches a function-scoped fixture once
    per test), so `ansina.db` is the real file the running process is reading and
    writing — the user is seeded directly into it via plain `sqlite3`, the same
    technique `test_migration_survives_a_restart` already uses, plus `ansina.auth.
    hashing` for the credential hash (see this module's docstring for why that one
    import is allowed).
    """
    db_path = tmp_path / "ansina.db"
    salt = new_token_salt()
    token_hash = hash_token(_E2E_READ_TOKEN, salt)
    with sqlite3.connect(db_path) as conn:
        user_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO users (id, username) VALUES (?, ?)", (user_id, "e2e-reader")
        )
        (role_id,) = conn.execute("SELECT id FROM roles WHERE slug = 'read'").fetchone()
        conn.execute(
            "INSERT INTO role_assignments (id, subject_type, subject_id, role_id) "
            "VALUES (?, 'user', ?, ?)",
            (uuid.uuid4().hex, user_id, role_id),
        )
        conn.execute(
            "INSERT INTO credentials (id, user_id, type, hash, salt) "
            "VALUES (?, ?, 'api_token', ?, ?)",
            (uuid.uuid4().hex, user_id, token_hash, salt),
        )
        conn.commit()

    # 401: no token at all.
    no_token_response = httpx.post(f"{authed_server}/heart/tick/pause")
    assert no_token_response.status_code == 401
    assert no_token_response.json()["code"] == "ansina.unauthorized"

    # 403: a valid Read-role token, but Read holds no grant for POST.
    read_headers = {"Authorization": f"Bearer {_E2E_READ_TOKEN}"}
    forbidden_response = httpx.post(
        f"{authed_server}/heart/tick/pause", headers=read_headers
    )
    assert forbidden_response.status_code == 403
    assert forbidden_response.headers["content-type"] == "application/problem+json"
    assert forbidden_response.json()["code"] == "ansina.forbidden"

    # 200: the same token, on the GET verb Read does grant.
    ok_response = httpx.get(f"{authed_server}/version", headers=read_headers)
    assert ok_response.status_code == 200
    assert ok_response.json()["name"] == "ansina"


def test_sudo_step_up_round_trip(authed_server: str, tmp_path: Path) -> None:
    """Issue #26's headline AC, black-box end to end: a `Maintain` caller is refused
    the sensitive break-glass route with no grant (403 `ansina.auth.sudo_required`),
    obtains one via `POST /auth/sudo`, succeeds with it (204), and is refused again
    once that grant is revoked — while an `Admin` token reaches the same route with no
    grant at all. `Maintain`/password seeded directly into the running server's SQLite
    file, the same technique `test_read_role_token_progresses_401_then_403_then_200`
    already establishes.
    """
    db_path = tmp_path / "ansina.db"
    salt = new_token_salt()
    token_hash = hash_token(_E2E_MAINTAIN_TOKEN, salt)
    password_hash = hash_password(_E2E_MAINTAIN_PASSWORD, _E2E_CHEAP_ARGON2)
    with sqlite3.connect(db_path) as conn:
        user_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO users (id, username) VALUES (?, ?)",
            (user_id, "e2e-maintainer"),
        )
        (role_id,) = conn.execute(
            "SELECT id FROM roles WHERE slug = 'maintain'"
        ).fetchone()
        conn.execute(
            "INSERT INTO role_assignments (id, subject_type, subject_id, role_id) "
            "VALUES (?, 'user', ?, ?)",
            (uuid.uuid4().hex, user_id, role_id),
        )
        conn.execute(
            "INSERT INTO credentials (id, user_id, type, hash, salt) "
            "VALUES (?, ?, 'api_token', ?, ?)",
            (uuid.uuid4().hex, user_id, token_hash, salt),
        )
        conn.execute(
            "INSERT INTO credentials (id, user_id, type, hash, salt) "
            "VALUES (?, ?, 'password', ?, NULL)",
            (uuid.uuid4().hex, user_id, password_hash),
        )
        conn.commit()

    maintain_headers = {"Authorization": f"Bearer {_E2E_MAINTAIN_TOKEN}"}

    # 403: Maintain's role grants DELETE on auth.sudo.grants, but there's no live
    # sudo grant yet.
    no_grant_response = httpx.delete(
        f"{authed_server}/auth/sudo/grants", headers=maintain_headers
    )
    assert no_grant_response.status_code == 403
    assert no_grant_response.json()["code"] == "ansina.auth.sudo_required"

    # Step up.
    step_up_response = httpx.post(
        f"{authed_server}/auth/sudo",
        headers=maintain_headers,
        json={"password": _E2E_MAINTAIN_PASSWORD},
    )
    assert step_up_response.status_code == 200
    grant_token = step_up_response.json()["token"]

    # 204: the same sensitive route, now presenting a live grant.
    granted_headers = {**maintain_headers, "X-Sudo-Token": grant_token}
    revoke_all_response = httpx.delete(
        f"{authed_server}/auth/sudo/grants", headers=granted_headers
    )
    assert revoke_all_response.status_code == 204

    # The break-glass call above revoked every active grant, including the one that
    # just authorized it — presenting it again is refused.
    again_response = httpx.delete(
        f"{authed_server}/auth/sudo/grants", headers=granted_headers
    )
    assert again_response.status_code == 403
    assert again_response.json()["code"] == "ansina.auth.sudo_required"

    # Admin (the bootstrap identity `authed_server` already authenticates as via
    # _E2E_TOKEN) reaches the same sensitive route with no grant at all, by design.
    admin_response = httpx.delete(
        f"{authed_server}/auth/sudo/grants",
        headers={"Authorization": f"Bearer {_E2E_TOKEN}"},
    )
    assert admin_response.status_code == 204


def test_bootstrap_token_is_generated_printed_once_and_authenticates(
    tmp_path: Path,
) -> None:
    """Issue #24 (redesign): first boot with `security.enabled` at its default
    (`true`) and no `ANSINA_SECURITY__API_TOKEN` configured auto-generates a
    bootstrap token, prints it to stdout exactly once, and that token immediately
    authenticates a real request — the whole generate -> print -> DB-backed-verify
    chain working end to end, not just its pieces in isolation.

    Captures the child's combined stdout/stderr to a file rather than a pipe:
    reading a live process's stdout pipe risks blocking on buffering, where a file
    can be read at any time without coordinating with the writer.
    """
    port = _free_port()
    (tmp_path / "ansina.toml").write_text(
        f'[server]\nhost = "127.0.0.1"\nport = {port}\n'
        f'[database]\npath = "{(tmp_path / "ansina.db").as_posix()}"\n',
        encoding="utf-8",
    )
    output_path = tmp_path / "server-output.log"
    base_url = f"http://127.0.0.1:{port}"

    with output_path.open("w", encoding="utf-8") as output_file:
        process = subprocess.Popen(  # fixed argv, no shell, no untrusted input
            [sys.executable, "-m", "ansina"],
            cwd=tmp_path,
            stdout=output_file,
            stderr=subprocess.STDOUT,
            env=os.environ,
        )
        try:
            deadline = time.monotonic() + _STARTUP_TIMEOUT_S
            last_error: Exception | None = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail(
                        f"ansina exited early (code {process.returncode}):\n"
                        f"{output_path.read_text(encoding='utf-8')}"
                    )
                try:
                    response = httpx.get(f"{base_url}/healthz", timeout=1.0)
                    if response.status_code == 200:
                        break
                except httpx.HTTPError as exc:
                    last_error = exc
                time.sleep(_POLL_INTERVAL_S)
            else:
                process.kill()
                raise TimeoutError(f"ansina never became healthy: {last_error}")

            output = output_path.read_text(encoding="utf-8")
            # The banner's token line (see `ansina.auth.bootstrap._BANNER`): exactly
            # three leading spaces, nothing else on the line.
            match = re.search(r"^   (\S+)$", output, re.MULTILINE)
            assert match is not None, f"bootstrap token banner not found:\n{output}"
            token = match.group(1)

            response = httpx.get(
                f"{base_url}/version", headers={"Authorization": f"Bearer {token}"}
            )
            assert response.status_code == 200
            assert response.json()["name"] == "ansina"

            response = httpx.get(
                f"{base_url}/version",
                headers={"Authorization": f"Bearer {token}x"},
            )
            assert response.status_code == 401

            # The raw token appears exactly once in the entire captured output — the
            # banner itself — never inside a JSON log line alongside it.
            assert output.count(token) == 1
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_migration_survives_a_restart(tmp_path: Path) -> None:
    """A real, black-box run of issue #6's acceptance criteria: on first boot the
    database file is created and migrated, and a second boot against the same file
    does not re-apply already-applied migrations — checked from outside the process,
    via a plain `sqlite3` connection to the file the server wrote.
    """
    with _launch_server(tmp_path) as srv:
        response = httpx.get(f"{srv.base_url}/readyz")
        assert response.json()["checks"]["database"] is True

    db_path = tmp_path / "ansina.db"
    assert db_path.exists()
    with sqlite3.connect(db_path) as conn:
        (journal_mode,) = conn.execute("PRAGMA journal_mode").fetchone()
        assert journal_mode.lower() == "wal"
        rows = conn.execute("SELECT version FROM schema_version").fetchall()
        # (1,) = storage's own bookkeeping table (issue #6); (2,) = the RBAC identity
        # model (issue #24); (3,) = sudo grants/lockouts (issue #26) — bump this
        # alongside `storage/migrations/` whenever a new migration lands.
        assert rows == [(1,), (2,), (3,)]

    # Boot again against the same tmp_path (same ansina.toml, same db file).
    with _launch_server(tmp_path) as srv:
        response = httpx.get(f"{srv.base_url}/readyz")
        assert response.json()["checks"]["database"] is True

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT version FROM schema_version").fetchall()
        # still exactly these rows — nothing re-applied
        assert rows == [(1,), (2,), (3,)]


def test_heart_enabled_without_a_viable_runtime_fails_loudly(tmp_path: Path) -> None:
    """Issue #10 AC #4: turning the Heart on with no viable adapter available must
    fail loudly at boot — never a silent no-op, never a bare traceback. This is
    platform-independent: CI never installs the `mlx` extra on either OS leg, so
    `mlx_lm` is never importable regardless of `sys.platform`. (Asserted on
    "non-zero exit + stderr mentions the heart," not the exact sentence, so this
    stays green on a Mac that *does* have the extra installed — there the failure is
    an absent model instead of an absent backend, per `ansina.heart.selection`.)
    """
    port = _free_port()
    (tmp_path / "ansina.toml").write_text(
        f'[server]\nhost = "127.0.0.1"\nport = {port}\n'
        f'[database]\npath = "{(tmp_path / "ansina.db").as_posix()}"\n',
        encoding="utf-8",
    )

    process = subprocess.run(
        [sys.executable, "-m", "ansina"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "ANSINA_HEART__ENABLED": "true"},
        timeout=_STARTUP_TIMEOUT_S,
    )

    assert process.returncode != 0
    combined = (process.stdout + process.stderr).lower()
    assert "heart" in combined
    assert "traceback" not in combined


def test_shuts_down_cleanly(tmp_path: Path) -> None:
    """Issue #16's M0 E2E coverage list includes "the process shuts down cleanly" —
    unlike every other test here, this one inspects the subprocess itself (exit code,
    captured output) rather than just its HTTP responses, since a stuck or crashing
    shutdown wouldn't show up in any response the server sent while still running.
    """
    with _launch_server(tmp_path) as srv:
        response = httpx.get(f"{srv.base_url}/healthz")
        assert response.status_code == 200
        process = srv.process

    # By the time `_launch_server`'s `finally` block returns control here, it has
    # already sent SIGTERM (`Popen.terminate()`) and waited for exit.
    assert process.returncode == -signal.SIGTERM
    output = process.stdout.read() if process.stdout else ""
    assert "Traceback" not in output
