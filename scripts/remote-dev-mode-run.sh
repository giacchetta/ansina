#!/usr/bin/env bash
# Runs on the bench host (the Mac Mini M4), inside the tmux session
# `scripts/remote-dev-mode.sh` creates, from the checkout it just force-synced to
# origin/<branch> — see issue #62. Deliberately thin: the real verification logic
# all lives in `scripts/dev-mode-smoke.sh`/`scripts/dev_mode_verify.py`, which this
# just runs on the real target hardware — the same way `scripts/remote-heart-run.sh`
# only wraps `make heart-bench`, so nothing is duplicated between the local and
# remote paths.
#
# Deliberately `set -uo pipefail` without `-e`, same reasoning as
# `remote-heart-run.sh`: a non-zero verdict must still be captured into `.exit`,
# never abort this script before that write happens.
set -uo pipefail

cd "$(dirname "$0")/.."

mkdir -p /tmp/ansina-dev-mode/reports

{
    git log -1 --format='dev-mode smoking %h (%s)'
    make dev-mode-smoke
} 2>&1 | tee /tmp/ansina-dev-mode/run.log

# Written last: scripts/remote-dev-mode.sh polls for this file's existence, so
# nothing it reads is ever a partial run.
echo "${PIPESTATUS[0]}" > /tmp/ansina-dev-mode/.exit
