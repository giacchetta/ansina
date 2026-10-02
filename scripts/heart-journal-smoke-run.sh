#!/usr/bin/env bash
# Runs on the bench host (the Mac Mini M4), inside the tmux session
# `scripts/heart-journal-smoke.sh` creates, from the checkout it just force-synced to
# origin/<branch> — see issue #55's Mac Mini acceptance check. Mirrors
# `scripts/remote-heart-run.sh`'s shape (tee everything to run.log, write the exit
# marker last) but boots the real daemon instead of the eval harness: with the Heart
# and tick loop enabled against a scratch database, it waits for a handful of real
# ticks, fetches `GET /heart/journal`, and stops the daemon — proving a tick's
# journal row matches what the daemon's own log line reported for that same tick.
#
# Deliberately `set -uo pipefail` without `-e`, same reasoning as
# `remote-heart-run.sh`: a failure here must be captured into `.exit`, never abort
# this script before that write happens.
set -uo pipefail

cd "$(dirname "$0")/.."

# Resolved the same way the Makefile resolves `$(UV)`: `uv` is installed to
# `~/.local/bin`, which (per `memory/mac-mini-heart-bench-host.md`) is not on this
# session's non-login PATH — `make heart-bench-sync` below works around that because
# Make expands its own `$(UV)` fallback, but the direct `uv run` call further down
# needs the same resolution done explicitly.
UV="$(command -v uv 2>/dev/null || echo "$HOME/.local/bin/uv")"

SCRATCH=/tmp/ansina-heart-journal-smoke
REPORTS="$SCRATCH/reports"
PORT="${ANSINA_SMOKE_PORT:-8099}"
TICK_INTERVAL="${ANSINA_SMOKE_TICK_INTERVAL:-5}"
# A plain positional argument, not an env var — see
# `scripts/heart-journal-smoke.sh`'s own comment on why this one value in
# particular is passed this way (both sides of the run need to agree on it, and
# tmux's env snapshot isn't a dependable carrier).
MIN_TICKS="${1:-3}"
STARTUP_TIMEOUT_SECONDS=60
TICK_TIMEOUT_SECONDS=120

mkdir -p "$REPORTS"
rm -f "$SCRATCH"/ansina.db*

{
    git log -1 --format='smoking %h (%s)'
    make heart-bench-sync

    export ANSINA_SECURITY__ENABLED=false
    export ANSINA_SERVER__PORT="$PORT"
    export ANSINA_DATABASE__PATH="$SCRATCH/ansina.db"
    export ANSINA_HEART__ENABLED=true
    export ANSINA_HEART__TICK__INTERVAL_SECONDS="$TICK_INTERVAL"
    export ANSINA_HEART__TICK__JITTER_SECONDS=0

    base="http://127.0.0.1:$PORT"

    "$UV" run --extra mlx python -m ansina &
    daemon_pid=$!
    trap 'kill "$daemon_pid" 2>/dev/null || true' EXIT

    echo "Waiting for the daemon to answer /healthz (timeout ${STARTUP_TIMEOUT_SECONDS}s) ..."
    waited=0
    until curl -fsS "$base/healthz" >/dev/null 2>&1; do
        if [ "$waited" -ge "$STARTUP_TIMEOUT_SECONDS" ]; then
            echo "Daemon never answered /healthz within ${STARTUP_TIMEOUT_SECONDS}s" >&2
            exit 1
        fi
        sleep 1
        waited=$((waited + 1))
    done

    echo "Waiting for at least $MIN_TICKS real tick(s) (timeout ${TICK_TIMEOUT_SECONDS}s) ..."
    waited=0
    ticks=0
    while [ "$ticks" -lt "$MIN_TICKS" ]; do
        if [ "$waited" -ge "$TICK_TIMEOUT_SECONDS" ]; then
            echo "Only $ticks tick(s) completed within ${TICK_TIMEOUT_SECONDS}s" >&2
            exit 1
        fi
        sleep 2
        waited=$((waited + 2))
        ticks="$(
            curl -fsS "$base/heart/tick" 2>/dev/null \
                | python3 -c 'import json,sys; print(json.load(sys.stdin)["ticks"])' \
                2>/dev/null || echo 0
        )"
        echo "ticks so far: $ticks"
    done

    echo "Fetching GET /heart/journal ..."
    curl -fsS "$base/heart/journal?limit=50" | python3 -m json.tool \
        > "$REPORTS/journal.json"

    echo "Stopping the daemon ..."
    kill "$daemon_pid"
    wait "$daemon_pid" 2>/dev/null || true
    trap - EXIT

    echo "--- journal.json ---"
    cat "$REPORTS/journal.json"
} 2>&1 | tee "$SCRATCH/run.log"
exit_code="${PIPESTATUS[0]}"

# Copied into reports/ (not just left at $SCRATCH/run.log) so the local driver's one
# `scp .../reports/*` fetches both the journal page and the log to cross-check it
# against, in a single round-trip.
cp "$SCRATCH/run.log" "$REPORTS/run.log"

# Written last: scripts/heart-journal-smoke.sh polls for this file's existence, so
# nothing it reads is ever a partial run.
echo "$exit_code" > "$SCRATCH/.exit"
