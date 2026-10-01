#!/usr/bin/env bash
# Runs on the bench host (the Mac Mini M4), inside the tmux session
# `scripts/remote-heart.sh` creates, from the checkout it just force-synced to
# origin/<branch> — see issue #58. Always the version of this script belonging
# to the branch under test: editing it requires pushing before the change takes
# effect remotely.
#
# Deliberately `set -uo pipefail` without `-e`: the bench's own non-zero
# gate-FAIL exit must be captured into .exit, never abort this script early.
set -uo pipefail

cd "$(dirname "$0")/.."

mkdir -p /tmp/ansina-heart-bench/reports

{
    git log -1 --format='benching %h (%s)'
    make heart-bench-sync
    make heart-bench ARGS="$* --out-dir /tmp/ansina-heart-bench/reports"
} 2>&1 | tee /tmp/ansina-heart-bench/run.log

# Written last: scripts/remote-heart.sh polls for this file's existence, so
# nothing it reads is ever a partial run.
echo "${PIPESTATUS[0]}" > /tmp/ansina-heart-bench/.exit
