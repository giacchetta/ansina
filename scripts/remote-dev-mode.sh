#!/usr/bin/env bash
# Local driver for `make remote-dev-mode` / `make remote-dev-mode-tail` /
# `make remote-dev-mode-attach` — issue #62's Mac Mini confirmation run. Mirrors
# `scripts/remote-heart.sh`'s shape exactly (issue #58's own pattern) — every
# ssh/pipe/detached-spawn command string lives in a script, never typed ad hoc —
# but drives a different remote script (`remote-dev-mode-run.sh`, which just runs
# `make dev-mode-smoke` for real) in its own tmux session so the tools never
# collide. There is no local artifact to fetch back afterward: the acceptance
# evidence is the objects in the bucket itself, verified in place by
# `scripts/dev_mode_verify.py` on the remote side.
#
# Config is entirely env-driven, read from `.envrc` (gitignored, holds live
# secrets — see `.agents/guardrails/forbidden-actions.md`'s no-env-tampering
# rule), reusing the exact same `ANSINA_REMOTE_HOST`/`ANSINA_REMOTE_PATH`
# variables `scripts/remote-heart.sh` already requires.
set -euo pipefail

SUBCOMMAND="${1:-run}"
[ $# -gt 0 ] && shift || true

REMOTE_SCRATCH="/tmp/ansina-dev-mode"
POLL_INTERVAL_SECONDS=5

ANSINA_REMOTE_SHELL="${ANSINA_REMOTE_SHELL:-zsh}"
ANSINA_REMOTE_SESSION="${ANSINA_DEV_MODE_SESSION:-ansina-dev-mode}"
ANSINA_REMOTE_TIMEOUT="${ANSINA_REMOTE_TIMEOUT:-300}"

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

# Same quoting helper as `scripts/remote-heart.sh` — the one place a command
# string sent over ssh is quoted, kept out of every call site below.
squote() {
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

remote_sh() {
    ssh "$ANSINA_REMOTE_HOST" "$ANSINA_REMOTE_SHELL -lc $(squote "$1")"
}

cmd_run() {
    require_config

    if [ -n "$(git status --porcelain)" ]; then
        echo "Local worktree is dirty. Commit or stash before running a remote dev-mode check." >&2
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
        echo "Run 'git push' first — the Mac Mini runs origin/$branch, never" \
             "an unpushed local commit." >&2
        exit 2
    fi

    echo "Syncing $ANSINA_REMOTE_HOST:$ANSINA_REMOTE_PATH to origin/$branch ..." >&2
    local sync_cmd
    sync_cmd="cd $(squote "$ANSINA_REMOTE_PATH") && git fetch --prune origin"
    sync_cmd+=" && git checkout -B $(squote "$branch") $(squote "origin/$branch")"
    sync_cmd+=" && git reset --hard $(squote "origin/$branch")"
    sync_cmd+=" && git clean -fd"
    remote_sh "$sync_cmd"

    echo "Launching dev-mode smoke in tmux session '$ANSINA_REMOTE_SESSION' ..." >&2
    local run_script="$ANSINA_REMOTE_PATH/scripts/remote-dev-mode-run.sh"
    local launch_cmd
    launch_cmd="rm -rf $(squote "$REMOTE_SCRATCH")"
    launch_cmd+=" && mkdir -p $(squote "$REMOTE_SCRATCH/reports")"
    launch_cmd+=" && (tmux kill-session -t $(squote "$ANSINA_REMOTE_SESSION") || true)"
    launch_cmd+=" && tmux new-session -d -s $(squote "$ANSINA_REMOTE_SESSION")"
    # tmux's own trailing argument is itself a shell-command string it later hands
    # to a shell — the one place a single hand-placed quote pair belongs, left for
    # *that* shell to interpret rather than re-escaped by squote() here.
    launch_cmd+=" '$run_script'"
    remote_sh "$launch_cmd"

    echo "Waiting for the run to finish (timeout ${ANSINA_REMOTE_TIMEOUT}s) ..." >&2
    local waited=0
    local exit_marker="$REMOTE_SCRATCH/.exit"
    while ! ssh "$ANSINA_REMOTE_HOST" "test -f $(squote "$exit_marker")"; do
        if [ "$waited" -ge "$ANSINA_REMOTE_TIMEOUT" ]; then
            cat >&2 <<EOF
Timed out after ${ANSINA_REMOTE_TIMEOUT}s waiting for the run to finish.
The run is still going on the Mac Mini — it was never killed. Check on it with:

  make remote-dev-mode-tail
  make remote-dev-mode-attach
EOF
            exit 1
        fi
        sleep "$POLL_INTERVAL_SECONDS"
        waited=$((waited + POLL_INTERVAL_SECONDS))
    done

    local exit_code
    exit_code="$(ssh "$ANSINA_REMOTE_HOST" "cat $(squote "$exit_marker")")"

    echo "--- run.log tail ---" >&2
    ssh "$ANSINA_REMOTE_HOST" "tail -n 80 $(squote "$REMOTE_SCRATCH/run.log")" >&2 || true
    echo "Dev-mode smoked $branch @ ${local_sha:0:12}" >&2
    if [ "$exit_code" = "0" ]; then
        echo "Verdict: PASS" >&2
    else
        echo "Verdict: FAIL (exit $exit_code)" >&2
    fi
    exit "$exit_code"
}

cmd_tail() {
    require_config
    local n="${N:-40}"
    ssh "$ANSINA_REMOTE_HOST" "tail -n $(squote "$n") $(squote "$REMOTE_SCRATCH/run.log")"
}

cmd_attach() {
    require_config
    exec ssh -t "$ANSINA_REMOTE_HOST" \
        "$ANSINA_REMOTE_SHELL -lc $(squote "tmux attach -t $ANSINA_REMOTE_SESSION")"
}

case "$SUBCOMMAND" in
    run) cmd_run "$@" ;;
    tail) cmd_tail "$@" ;;
    attach) cmd_attach "$@" ;;
    *)
        echo "Unknown subcommand: $SUBCOMMAND (expected run|tail|attach)" >&2
        exit 2
        ;;
esac
