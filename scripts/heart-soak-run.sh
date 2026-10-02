#!/usr/bin/env bash
# Runs on the bench host (the Mac Mini M4), inside the tmux session
# `scripts/heart-soak.sh start` creates, from the checkout it just force-synced to
# origin/<branch> — issue #56's multi-hour heartbeat soak. Mirrors
# `scripts/heart-journal-smoke-run.sh`'s shape (tee everything to run.log, write the
# exit marker last) but runs far longer and, unlike that script's "wait for a handful
# of ticks and stop," samples RSS + the tick loop's own live state on a fixed cadence
# for the whole run — the "no unbounded RSS growth, no tick-duration drift" evidence
# issue #56's AC asks for has to come from many hours of samples, not a few ticks.
#
# Deliberately `set -uo pipefail` without `-e`, same reasoning as every other
# `*-run.sh` script here: a failure partway through an 8-hour run must still be
# captured into `.exit`, never abort this script before that write happens.
set -uo pipefail

cd "$(dirname "$0")/.."

# Resolved the same way `remote-heart-run.sh`/`heart-journal-smoke-run.sh` resolve
# it: `uv` lives at `~/.local/bin/uv`, not on this session's non-login PATH (see
# memory/mac-mini-heart-bench-host.md).
UV="$(command -v uv 2>/dev/null || echo "$HOME/.local/bin/uv")"

SCRATCH=/tmp/ansina-heart-soak
REPORTS="$SCRATCH/reports"
SAMPLES_FILE="$SCRATCH/samples.jsonl"

PORT="${ANSINA_SOAK_PORT:-8098}"
# `HOURS` is a plain positional argument, not an env var ahead of `tmux new-session`
# — same reasoning scripts/heart-journal-smoke.sh's own comment documents (tmux's
# environment is a snapshot tied to its own server lifecycle, not reliably this
# client's env). May be fractional (e.g. "0.05" for a quick end-to-end validation
# run) — computed via python3 below rather than bash integer arithmetic.
HOURS="${1:-${ANSINA_SOAK_HOURS:-8}}"
SAMPLE_INTERVAL="${ANSINA_SOAK_SAMPLE_INTERVAL:-60}"
TICK_INTERVAL="${ANSINA_SOAK_TICK_INTERVAL:-30}"
# The Mac-validated profile's own jitter value (docs/heart/mac-mini-m4.toml) — not a
# separate knob: a soak exists to measure real-deployment scheduling drift, so jitter
# stays on rather than being disabled for convenience.
TICK_JITTER=3.0
# HeartSettings' own validated default (docs/heart/mac-mini-m4.toml, issue #53's
# bench ladder) — named explicitly here rather than left to the daemon's own default
# so a renamed/repointed default can never silently change what a soak measures
# without this script's own metadata line changing to match.
MODEL_REPO="mlx-community/gemma-4-e2b-it-4bit"
STARTUP_TIMEOUT_SECONDS=60

mkdir -p "$REPORTS"
rm -f "$SCRATCH"/ansina.db* "$SAMPLES_FILE"

{
    echo "branch: $(git rev-parse --abbrev-ref HEAD)"
    git log -1 --format='soaking %h (%s)'
    echo "soak config: model_repo=$MODEL_REPO prompt_variant=strict" \
         "interval_seconds=$TICK_INTERVAL jitter_seconds=$TICK_JITTER hours=$HOURS" \
         "sample_interval_seconds=$SAMPLE_INTERVAL"
    make heart-bench-sync

    export ANSINA_SECURITY__ENABLED=false
    export ANSINA_SERVER__PORT="$PORT"
    export ANSINA_DATABASE__PATH="$SCRATCH/ansina.db"
    export ANSINA_HEART__ENABLED=true
    export ANSINA_HEART__MODEL_REPO="$MODEL_REPO"
    export ANSINA_HEART__TICK__INTERVAL_SECONDS="$TICK_INTERVAL"
    export ANSINA_HEART__TICK__JITTER_SECONDS="$TICK_JITTER"

    base="http://127.0.0.1:$PORT"

    "$UV" run --extra mlx python -m ansina &
    daemon_pid=$!

    sampler_pid=""
    cleanup() {
        [ -n "$sampler_pid" ] && kill "$sampler_pid" 2>/dev/null || true
        kill "$daemon_pid" 2>/dev/null || true
    }
    trap cleanup EXIT

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

    # The backgrounded PID is `uv run`'s own wrapper process, not necessarily the
    # real MLX/python process — resolve its child if one exists, falling back to the
    # wrapper PID itself (covers a `uv run` that execs in place, leaving no child).
    model_pid="$(pgrep -P "$daemon_pid" 2>/dev/null | head -n1)"
    model_pid="${model_pid:-$daemon_pid}"

    end_epoch="$(python3 -c \
        "import time,sys; print(int(time.time() + float(sys.argv[1]) * 3600))" \
        "$HOURS")"
    echo "Sampling pid $model_pid every ${SAMPLE_INTERVAL}s until $(date -r "$end_epoch" 2>/dev/null || date -d "@$end_epoch" 2>/dev/null || echo "$end_epoch") ..."

    start_epoch=$(date +%s)

    sample_once() {
        local elapsed rss_kib tick_json
        elapsed=$(( $(date +%s) - start_epoch ))
        rss_kib="$(ps -o rss= -p "$model_pid" 2>/dev/null | tr -d ' ')"
        tick_json="$(curl -fsS "$base/heart/tick" 2>/dev/null || echo '{}')"
        python3 -c '
import json, sys, time
elapsed = int(sys.argv[1])
rss_kib = int(sys.argv[2]) if sys.argv[2] else 0
try:
    tick = json.loads(sys.argv[3])
except (json.JSONDecodeError, IndexError):
    tick = {}
print(json.dumps({
    "t": time.time(),
    "elapsed_s": elapsed,
    "rss_kib": rss_kib,
    "ticks": tick.get("ticks"),
    "paused": tick.get("paused"),
    "paused_reason": tick.get("paused_reason"),
    "last_decision": tick.get("last_decision"),
    "last_duration_seconds": tick.get("last_duration_seconds"),
}))
' "$elapsed" "$rss_kib" "$tick_json" >> "$SAMPLES_FILE"
    }

    (
        while [ "$(date +%s)" -lt "$end_epoch" ]; do
            sample_once
            sleep "$SAMPLE_INTERVAL"
        done
    ) &
    sampler_pid=$!

    echo "Waiting for the ${HOURS}h soak window to elapse ..."
    wait "$sampler_pid" 2>/dev/null || true
    sampler_pid=""

    echo "Soak window elapsed. Taking a final sample ..."
    sample_once

    echo "Fetching GET /heart/journal (most recent page) ..."
    curl -fsS "$base/heart/journal?limit=500" | python3 -m json.tool \
        > "$REPORTS/journal.json"

    echo "Stopping the daemon ..."
    kill "$daemon_pid" 2>/dev/null || true
    wait "$daemon_pid" 2>/dev/null || true
    trap - EXIT

    echo "--- samples recorded: $(wc -l < "$SAMPLES_FILE" | tr -d ' ') ---"
} 2>&1 | tee "$SCRATCH/run.log"
exit_code="${PIPESTATUS[0]}"

# Copied into reports/ (not just left at $SCRATCH) so the local driver's one
# `scp .../reports/*` fetches everything `heart_soak_report.py` needs in a single
# round-trip.
cp "$SCRATCH/run.log" "$REPORTS/run.log"
cp "$SAMPLES_FILE" "$REPORTS/samples.jsonl" 2>/dev/null || true

# Written last: scripts/heart-soak.sh polls for this file's existence, so nothing it
# reads is ever a partial run.
echo "$exit_code" > "$SCRATCH/.exit"
