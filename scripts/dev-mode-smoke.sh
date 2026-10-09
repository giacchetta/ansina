#!/usr/bin/env bash
# `make dev-mode-smoke` — the primary verification for issue #62's Dev Mode: boots
# `ansina --dev` against a *scratch* database and spool dir, with whatever real
# [telemetry]/[telemetry.s3] credentials this host already has configured (the
# same `.envrc`/`ansina.toml` `make heart-bench-publish` already uses), waits for
# the daemon to produce real samples/log lines, waits for Vector to flush them to
# the bucket, stops the daemon, then verifies objects actually landed by reading
# them back out of the bucket directly — never trusting Vector's exit code or its
# own logs alone (the issue's own AC).
#
# Runs unchanged on this Linux dev box and on the Mac Mini: `make remote-dev-mode`
# just runs this exact target over there, in its own tmux session (the same way
# `scripts/remote-heart-run.sh` only wraps `make heart-bench`) — Dev Mode needs no
# GPU and no Heart, since `TelemetrySampler` runs independently of
# `[heart]`/`[heart.tick]` by design.
#
# Uploads under an isolated `_devmode-smoke/<host>-<utc-ts>/` key prefix so a
# smoke run can never collide with or pollute the real bench/soak corpus, and is
# trivially deletable afterward — `dev_mode_verify.py` prints the exact prefix to
# delete.
#
# Deliberately `set -uo pipefail` without `-e`, the same reasoning
# `remote-heart-run.sh`/`heart-journal-smoke-run.sh` already document: a failure
# here must still reach the final diagnostic output (the run.log tail), not abort
# the script before it's printed.
set -uo pipefail

cd "$(dirname "$0")/.."

UV="$(command -v uv 2>/dev/null || echo "$HOME/.local/bin/uv")"

SCRATCH="${TMPDIR:-/tmp}/ansina-dev-mode-smoke"
PORT="${ANSINA_SMOKE_PORT:-8098}"
SAMPLE_INTERVAL="${ANSINA_SMOKE_SAMPLE_INTERVAL:-2}"
STARTUP_TIMEOUT_SECONDS=60
SAMPLE_TIMEOUT_SECONDS=30
# Vector's own aws_s3 sink batch.timeout_secs is tuned to 5s in deploy/vector.toml
# specifically so a lab/smoke run like this one doesn't need to wait minutes for a
# flush — this just adds headroom for the disk-buffer write + the actual PUT.
FLUSH_WAIT_SECONDS="${ANSINA_SMOKE_FLUSH_WAIT:-15}"

HOST_TAG="$(hostname -s 2>/dev/null || hostname)"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
SMOKE_PREFIX="_devmode-smoke/${HOST_TAG}-${TS}/"

rm -rf "$SCRATCH"
mkdir -p "$SCRATCH"

# Sources .envrc if present — the same guarded, read-only line
# `remote-heart-run.sh` already uses, not a "no env tampering" violation (reads a
# file the human already placed, never writes one). On the Mac Mini this is what
# actually carries the real R2 credentials into this process.
[ -f .envrc ] && . ./.envrc

export ANSINA_SECURITY__ENABLED=false
export ANSINA_SERVER__PORT="$PORT"
export ANSINA_DATABASE__PATH="$SCRATCH/ansina.db"
export ANSINA_TELEMETRY__ENABLED=true
export ANSINA_TELEMETRY__SPOOL_DIR="$SCRATCH/spool"
export ANSINA_TELEMETRY__SAMPLE_INTERVAL_SECONDS="$SAMPLE_INTERVAL"
export ANSINA_TELEMETRY__S3__KEY_PREFIX="$SMOKE_PREFIX"

base="http://127.0.0.1:$PORT"

echo "Smoke prefix: $SMOKE_PREFIX" >&2

"$UV" run python -m ansina --dev > "$SCRATCH/run.log" 2>&1 &
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

echo "Waiting for samples.jsonl to have content (timeout ${SAMPLE_TIMEOUT_SECONDS}s) ..." >&2
waited=0
samples_path="$SCRATCH/spool/samples.jsonl"
until [ -s "$samples_path" ]; do
    if [ "$waited" -ge "$SAMPLE_TIMEOUT_SECONDS" ]; then
        echo "samples.jsonl never appeared with content within ${SAMPLE_TIMEOUT_SECONDS}s" >&2
        cat "$SCRATCH/run.log" >&2
        exit 1
    fi
    sleep 1
    waited=$((waited + 1))
done

echo "Waiting ${FLUSH_WAIT_SECONDS}s for Vector to flush to the bucket ..." >&2
sleep "$FLUSH_WAIT_SECONDS"

echo "Stopping the daemon ..." >&2
kill "$daemon_pid" 2>/dev/null || true
wait "$daemon_pid" 2>/dev/null || true
trap - EXIT

echo "--- run.log tail ---" >&2
tail -n 60 "$SCRATCH/run.log" >&2

echo "Verifying objects landed in the bucket (reading them back out of it) ..." >&2
"$UV" run python scripts/dev_mode_verify.py
exit_code=$?

if [ "$exit_code" = "0" ]; then
    echo "Dev Mode smoke: PASS" >&2
else
    echo "Dev Mode smoke: FAIL (exit $exit_code)" >&2
fi
exit "$exit_code"
