#!/usr/bin/env bash
# Local driver for `make remote-heart-soak-start` / `-status` / `-fetch` / `-tail` /
# `-attach` / `-stop` — issue #56's multi-hour heartbeat soak. Mirrors
# `scripts/heart-journal-smoke.sh`'s shape (force-sync a pushed branch, launch in its
# own tmux session, every ssh/pipe/detached-spawn command string lives here, never
# typed ad hoc) with one structural difference: a soak runs for hours, so `start`
# launches it and returns immediately instead of blocking on a poll loop the way the
# existing (short) drivers do — `status`/`fetch` are separate, later invocations,
# deliberately spannable across local sessions (see docs/heart/soak.md): the soak runs
# detached on the Mac Mini regardless of whether this laptop stays on.
#
# Config is entirely env-driven, read from `.envrc` (gitignored, holds live secrets —
# see `.agents/guardrails/forbidden-actions.md`'s no-env-tampering rule), reusing the
# exact same `ANSINA_REMOTE_HOST`/`ANSINA_REMOTE_PATH` variables
# `scripts/remote-heart.sh` and `scripts/heart-journal-smoke.sh` already require.
set -euo pipefail

SUBCOMMAND="${1:-start}"
[ $# -gt 0 ] && shift || true

REMOTE_SCRATCH="/tmp/ansina-heart-soak"
LOCAL_DIR="/tmp/ansina-heart-soak"
DEST_DIR="docs/heart/soak"

ANSINA_REMOTE_SHELL="${ANSINA_REMOTE_SHELL:-zsh}"
ANSINA_REMOTE_SESSION="${ANSINA_SOAK_SESSION:-ansina-soak}"
HOURS="${ANSINA_SOAK_HOURS:-8}"

require_config() {
    local missing=0
    if [ -z "${ANSINA_REMOTE_HOST:-}" ]; then
        echo "ANSINA_REMOTE_HOST is not set." >&2
        missing=1
    fi
    if [ -z "${ANSINA_REMOTE_PATH:-}" ]; then
        echo "ANSINA_REMOTE_PATH is not set." >&2
        missing=1
    fi
    if [ "$missing" -ne 0 ]; then
        cat >&2 <<'EOF'

Add the following to your .envrc (gitignored — not written by this script; the same
two variables scripts/remote-heart.sh already requires):

  export ANSINA_REMOTE_HOST=<user@host>
  export ANSINA_REMOTE_PATH=<path to the ansina checkout on that host>
EOF
        exit 2
    fi
}

# Same quoting helper as scripts/remote-heart.sh / heart-journal-smoke.sh — the one
# place a command string sent over ssh is quoted, kept out of every call site below.
squote() {
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

remote_sh() {
    ssh "$ANSINA_REMOTE_HOST" "$ANSINA_REMOTE_SHELL -lc $(squote "$1")"
}

cmd_start() {
    require_config

    if [ -n "$(git status --porcelain)" ]; then
        echo "Local worktree is dirty. Commit or stash before starting a remote soak." >&2
        exit 2
    fi

    local branch
    branch="$(git rev-parse --abbrev-ref HEAD)"
    git fetch origin "$branch" >&2

    local local_sha remote_sha
    local_sha="$(git rev-parse HEAD)"
    remote_sha="$(git rev-parse "origin/$branch")"
    if [ "$local_sha" != "$remote_sha" ]; then
        echo "HEAD ($local_sha) is not pushed to origin/$branch ($remote_sha)." >&2
        echo "Run 'git push' first — the Mac Mini soaks origin/$branch, never an" \
             "unpushed local commit." >&2
        exit 2
    fi

    echo "Syncing $ANSINA_REMOTE_HOST:$ANSINA_REMOTE_PATH to origin/$branch ..." >&2
    local sync_cmd
    sync_cmd="cd $(squote "$ANSINA_REMOTE_PATH") && git fetch --prune origin"
    sync_cmd+=" && git checkout -B $(squote "$branch") $(squote "origin/$branch")"
    sync_cmd+=" && git reset --hard $(squote "origin/$branch")"
    sync_cmd+=" && git clean -fd"
    remote_sh "$sync_cmd"

    echo "Launching an ${HOURS}h soak in tmux session '$ANSINA_REMOTE_SESSION' ..." >&2
    local run_script="$ANSINA_REMOTE_PATH/scripts/heart-soak-run.sh"
    local launch_cmd
    launch_cmd="rm -rf $(squote "$REMOTE_SCRATCH")"
    launch_cmd+=" && mkdir -p $(squote "$REMOTE_SCRATCH/reports")"
    launch_cmd+=" && (tmux kill-session -t $(squote "$ANSINA_REMOTE_SESSION") || true)"
    launch_cmd+=" && tmux new-session -d -s $(squote "$ANSINA_REMOTE_SESSION")"
    # `HOURS` is a plain positional argument, not an env var ahead of `tmux
    # new-session` — same reasoning scripts/heart-journal-smoke.sh's own comment on
    # this exact line shape already documents (tmux's environment is a snapshot tied
    # to its own server lifecycle, not reliably this client's env).
    #
    # tmux's own trailing argument is itself a shell-command string it later hands to
    # a shell — the one place a hand-placed quote pair belongs, left for *that* shell
    # to interpret rather than re-escaped by squote() here (mirrors
    # remote-heart.sh's/heart-journal-smoke.sh's own comment on this exact line shape).
    launch_cmd+=" '$run_script $HOURS'"
    remote_sh "$launch_cmd"

    cat >&2 <<EOF
Soak launched — this call returns immediately, it does not wait for ${HOURS}h to pass.

  make remote-heart-soak-status   # is it still running? how far in?
  make remote-heart-soak-tail     # tail the live log
  make remote-heart-soak-attach   # attach to the tmux session interactively
  make remote-heart-soak-fetch    # once finished: fetch + render the report
  make remote-heart-soak-stop     # end it early

This can safely span multiple local sessions — the soak runs detached on the Mac
Mini regardless of whether this laptop stays on.
EOF
}

cmd_status() {
    require_config
    local exit_marker="$REMOTE_SCRATCH/.exit"
    if ssh "$ANSINA_REMOTE_HOST" "test -f $(squote "$exit_marker")"; then
        local exit_code
        exit_code="$(ssh "$ANSINA_REMOTE_HOST" "cat $(squote "$exit_marker")")"
        echo "Finished (exit $exit_code). Run 'make remote-heart-soak-fetch'."
        return 0
    fi
    if ! remote_sh "tmux has-session -t $(squote "$ANSINA_REMOTE_SESSION") 2>/dev/null"; then
        echo "Not running — no tmux session '$ANSINA_REMOTE_SESSION' and no .exit marker."
        return 1
    fi
    echo "Still running. Last few samples:"
    ssh "$ANSINA_REMOTE_HOST" "tail -n 5 $(squote "$REMOTE_SCRATCH/samples.jsonl")" 2>/dev/null \
        || echo "(no samples yet — still starting up)"
}

cmd_fetch() {
    require_config
    local exit_marker="$REMOTE_SCRATCH/.exit"
    if ! ssh "$ANSINA_REMOTE_HOST" "test -f $(squote "$exit_marker")"; then
        echo "The soak hasn't finished yet (no .exit marker on the remote)." >&2
        echo "Check 'make remote-heart-soak-status' first." >&2
        exit 2
    fi

    echo "Fetching run.log + samples.jsonl + journal.json back ..." >&2
    rm -rf "$LOCAL_DIR"
    mkdir -p "$LOCAL_DIR"
    scp -q "$ANSINA_REMOTE_HOST:$REMOTE_SCRATCH/reports/*" "$LOCAL_DIR/" 2>/dev/null || true

    local exit_code
    exit_code="$(ssh "$ANSINA_REMOTE_HOST" "cat $(squote "$exit_marker")")"
    if [ "$exit_code" != "0" ]; then
        echo "Remote run itself failed (exit $exit_code) — see $LOCAL_DIR/run.log." >&2
    fi

    echo "Rendering the report into $DEST_DIR/ (gitignored — see docs/heart/soak.md) ..." >&2
    mkdir -p "$DEST_DIR"
    uv run python scripts/heart_soak_report.py \
        "$LOCAL_DIR/run.log" "$LOCAL_DIR/samples.jsonl" "$LOCAL_DIR/journal.json" \
        "$DEST_DIR"
}

cmd_tail() {
    require_config
    local n="${N:-40}"
    ssh "$ANSINA_REMOTE_HOST" "tail -n $(squote "$n") $(squote "$REMOTE_SCRATCH/run.log")" 2>/dev/null \
        || echo "No run.log yet — the soak may still be starting." >&2
}

cmd_attach() {
    require_config
    exec ssh -t "$ANSINA_REMOTE_HOST" \
        "$ANSINA_REMOTE_SHELL -lc $(squote "tmux attach -t $ANSINA_REMOTE_SESSION")"
}

cmd_stop() {
    require_config
    remote_sh "tmux kill-session -t $(squote "$ANSINA_REMOTE_SESSION") || true"
    echo "Session '$ANSINA_REMOTE_SESSION' ended. Any process it started on the" \
         "Mac Mini (the daemon, the sampler loop) dies with it." >&2
}

case "$SUBCOMMAND" in
    start) cmd_start "$@" ;;
    status) cmd_status "$@" ;;
    fetch) cmd_fetch "$@" ;;
    tail) cmd_tail "$@" ;;
    attach) cmd_attach "$@" ;;
    stop) cmd_stop "$@" ;;
    *)
        echo "Unknown subcommand: $SUBCOMMAND (expected start|status|fetch|tail|attach|stop)" >&2
        exit 2
        ;;
esac
