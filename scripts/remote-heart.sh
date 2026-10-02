#!/usr/bin/env bash
# Local driver for `make remote-heart` / `make remote-heart-tail` /
# `make remote-heart-attach` — see issue #58. The agent's entire surface for a
# real-hardware Heart bench is this one script invoked through `make`; every
# ssh/pipe/detached-spawn command string lives here, never typed ad hoc.
#
# Config is entirely env-driven (the Makefile passes nothing but $ARGS) and read
# from `.envrc`, which this script never writes (gitignored, holds live secrets —
# see .agents/guardrails/forbidden-actions.md's no-env-tampering rule).
set -euo pipefail

SUBCOMMAND="${1:-run}"
[ $# -gt 0 ] && shift || true

REMOTE_SCRATCH="/tmp/ansina-heart-bench"
DEST_DIR="docs/heart/bench"
POLL_INTERVAL_SECONDS=5

ANSINA_REMOTE_SHELL="${ANSINA_REMOTE_SHELL:-zsh}"
ANSINA_REMOTE_SESSION="${ANSINA_REMOTE_SESSION:-ansina-bench}"
ANSINA_REMOTE_TIMEOUT="${ANSINA_REMOTE_TIMEOUT:-1800}"

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

Add the following to your .envrc (gitignored — not written by this script):

  export ANSINA_REMOTE_HOST=<user@host>
  export ANSINA_REMOTE_PATH=<path to the ansina checkout on that host>

Optional, with their defaults:

  export ANSINA_REMOTE_SHELL=zsh
  export ANSINA_REMOTE_SESSION=ansina-bench
  export ANSINA_REMOTE_TIMEOUT=1800
EOF
        exit 2
    fi
}

# Wraps $1 in single quotes, escaping any single quotes it already contains, so
# it survives as exactly one word when a POSIX shell parses it — the one place
# command strings sent over ssh are quoted, kept out of every call site below.
squote() {
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

remote_sh() {
    # Runs $1 on $ANSINA_REMOTE_HOST through a login shell, so PATH includes
    # tmux/uv the way a bare `ssh host 'cmd'` does not (see
    # memory/mac-mini-heart-bench-host.md — tmux and uv are login-shell-PATH-only).
    ssh "$ANSINA_REMOTE_HOST" "$ANSINA_REMOTE_SHELL -lc $(squote "$1")"
}

cmd_run() {
    require_config

    if [ -n "$(git status --porcelain)" ]; then
        echo "Local worktree is dirty. Commit or stash before running a remote bench." >&2
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
        echo "Run 'git push' first — the Mac Mini benches origin/$branch, never" \
             "an unpushed local commit." >&2
        exit 2
    fi

    echo "Syncing $ANSINA_REMOTE_HOST:$ANSINA_REMOTE_PATH to origin/$branch ..." >&2
    local sync_cmd
    sync_cmd="cd $(squote "$ANSINA_REMOTE_PATH") && git fetch --prune origin"
    sync_cmd+=" && git checkout -B $(squote "$branch") $(squote "origin/$branch")"
    # `checkout -B` alone only forces the working tree when the branch ref
    # actually moves — a re-run against an unchanged origin leaves a prior
    # local edit to a tracked file untouched (verified: reproduced live against
    # the Mac Mini). `reset --hard` unconditionally forces it regardless.
    sync_cmd+=" && git reset --hard $(squote "origin/$branch")"
    sync_cmd+=" && git clean -fd"
    remote_sh "$sync_cmd"

    echo "Launching bench in tmux session '$ANSINA_REMOTE_SESSION' ..." >&2
    local run_script="$ANSINA_REMOTE_PATH/scripts/remote-heart-run.sh"
    # tmux's own trailing argument is itself a shell-command string it later hands
    # to a shell — the one place a single hand-placed quote pair belongs, left for
    # *that* shell to interpret rather than re-escaped by squote() here.
    local launch_cmd
    launch_cmd="rm -rf $(squote "$REMOTE_SCRATCH")"
    launch_cmd+=" && mkdir -p $(squote "$REMOTE_SCRATCH/reports")"
    launch_cmd+=" && (tmux kill-session -t $(squote "$ANSINA_REMOTE_SESSION") || true)"
    launch_cmd+=" && tmux new-session -d -s $(squote "$ANSINA_REMOTE_SESSION")"
    launch_cmd+=" '$run_script $*'"
    remote_sh "$launch_cmd"

    echo "Waiting for the bench to finish (timeout ${ANSINA_REMOTE_TIMEOUT}s) ..." >&2
    local waited=0
    local exit_marker="$REMOTE_SCRATCH/.exit"
    while ! ssh "$ANSINA_REMOTE_HOST" "test -f $(squote "$exit_marker")"; do
        if [ "$waited" -ge "$ANSINA_REMOTE_TIMEOUT" ]; then
            cat >&2 <<EOF
Timed out after ${ANSINA_REMOTE_TIMEOUT}s waiting for the bench to finish.
The run is still going on the Mac Mini — it was never killed. Check on it with:

  make remote-heart-tail
  make remote-heart-attach
EOF
            exit 1
        fi
        sleep "$POLL_INTERVAL_SECONDS"
        waited=$((waited + POLL_INTERVAL_SECONDS))
    done

    local exit_code
    exit_code="$(ssh "$ANSINA_REMOTE_HOST" "cat $(squote "$exit_marker")")"

    echo "Fetching reports back ..." >&2
    local tmp_dir
    tmp_dir="$(mktemp -d)"
    trap 'rm -rf "$tmp_dir"' EXIT
    scp -q "$ANSINA_REMOTE_HOST:$REMOTE_SCRATCH/reports/*" "$tmp_dir/" 2>/dev/null || true

    mkdir -p "$DEST_DIR"
    local src stem suffix n dest_md dest_json
    for src in "$tmp_dir"/*.md; do
        [ -e "$src" ] || continue
        stem="$(basename "$src" .md)"
        if [ ! -e "$DEST_DIR/$stem.md" ] && [ ! -e "$DEST_DIR/$stem.json" ]; then
            suffix=""
        else
            n=2
            while [ -e "$DEST_DIR/${stem}-$n.md" ] || [ -e "$DEST_DIR/${stem}-$n.json" ]; do
                n=$((n + 1))
            done
            suffix="-$n"
        fi
        dest_md="$DEST_DIR/${stem}${suffix}.md"
        dest_json="$DEST_DIR/${stem}${suffix}.json"
        cp "$tmp_dir/$stem.md" "$dest_md"
        cp "$tmp_dir/$stem.json" "$dest_json"
        echo "Wrote $dest_md"
        echo "Wrote $dest_json"
    done

    echo "--- run.log tail ---" >&2
    ssh "$ANSINA_REMOTE_HOST" "tail -n 40 $(squote "$REMOTE_SCRATCH/run.log")" >&2 || true
    echo "Benched $branch @ ${local_sha:0:12}" >&2
    if [ "$exit_code" = "0" ]; then
        echo "Gate: PASS" >&2
    else
        echo "Gate: FAIL (exit $exit_code)" >&2
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
