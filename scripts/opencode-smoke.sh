#!/usr/bin/env bash
# `make opencode-smoke` — the primary hand-run verification for issue #77's
# /opencode/runs submission API: boots a plain `ansina` daemon (no Vector, no
# bucket — this is local-only, [opencode]/[telemetry] against a scratch spool
# dir) with [opencode] enabled against a REAL, already-installed `opencode`
# binary (see `make opencode-install`/docs/opencode.md), submits one real run
# over HTTP, polls it to a terminal status, and verifies the spooled
# kind=opencode/ telemetry file on disk carries the expected lifecycle events —
# never trusting the HTTP 202 alone. Mirrors `scripts/dev-mode-smoke.sh`'s own
# shape, minus that script's Vector/bucket leg — this check never ships
# anywhere; it only has to prove the daemon's own in-process behavior against
# a real binary.
#
# Needs a real `opencode` binary on PATH — neither CI leg has one, so (same
# reasoning `heart-bench`/`dev-mode-smoke` already document) this is a
# hand-run check, excluded from `check`/`check-all`. Pin
# ANSINA_OPENCODE__REQUIRED_VERSION_PREFIX yourself once you've installed and
# validated a specific release (see docs/opencode.md) — left unset (the
# default) accepts whatever `opencode --version` reports.
#
# Deliberately `set -uo pipefail` without `-e`, the same reasoning
# `remote-heart-run.sh`/`dev-mode-smoke.sh` already document: a failure here
# must still reach the final diagnostic output (the run.log tail), not abort
# the script before it's printed.
set -uo pipefail

cd "$(dirname "$0")/.."

UV="$(command -v uv 2>/dev/null || echo "$HOME/.local/bin/uv")"

SCRATCH="${TMPDIR:-/tmp}/ansina-opencode-smoke"
PORT="${ANSINA_SMOKE_PORT:-8099}"
STARTUP_TIMEOUT_SECONDS=30

rm -rf "$SCRATCH"
mkdir -p "$SCRATCH"

export ANSINA_SECURITY__ENABLED=false
export ANSINA_SERVER__HOST=127.0.0.1
export ANSINA_SERVER__PORT="$PORT"
export ANSINA_DATABASE__PATH="$SCRATCH/ansina.db"
export ANSINA_OPENCODE__ENABLED=true
export ANSINA_OPENCODE__WORKSPACE_DIR="$SCRATCH/workspace"
export ANSINA_TELEMETRY__ENABLED=true
export ANSINA_TELEMETRY__SPOOL_DIR="$SCRATCH/spool"

base="http://127.0.0.1:$PORT"

"$UV" run python -m ansina >"$SCRATCH/run.log" 2>&1 &
daemon_pid=$!
trap 'kill "$daemon_pid" 2>/dev/null || true' EXIT

echo "Waiting for the daemon to answer /healthz (timeout ${STARTUP_TIMEOUT_SECONDS}s) ..." >&2
waited=0
until curl -fsS "$base/healthz" >/dev/null 2>&1; do
    if [ "$waited" -ge "$STARTUP_TIMEOUT_SECONDS" ]; then
        echo "Daemon never answered /healthz within ${STARTUP_TIMEOUT_SECONDS}s" >&2
        cat "$SCRATCH/run.log" >&2
        exit 1
    fi
    if ! kill -0 "$daemon_pid" 2>/dev/null; then
        echo "Daemon process exited early — see run.log below" >&2
        cat "$SCRATCH/run.log" >&2
        exit 1
    fi
    sleep 1
    waited=$((waited + 1))
done

echo "Submitting a real run and polling it to completion ..." >&2
"$UV" run python scripts/opencode_smoke_verify.py
exit_code=$?

echo "Stopping the daemon ..." >&2
kill "$daemon_pid" 2>/dev/null || true
wait "$daemon_pid" 2>/dev/null || true
trap - EXIT

echo "--- run.log tail ---" >&2
tail -n 60 "$SCRATCH/run.log" >&2

if [ "$exit_code" = "0" ]; then
    echo "OpenCode smoke: PASS" >&2
else
    echo "OpenCode smoke: FAIL (exit $exit_code)" >&2
fi
exit "$exit_code"
