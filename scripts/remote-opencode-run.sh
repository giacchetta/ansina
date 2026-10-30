#!/usr/bin/env bash
# Runs on the bench host, inside the tmux session `scripts/remote-opencode.sh`
# creates, from the checkout it just force-synced to origin/<branch> — see
# issue #77. Deliberately thin: the real verification logic all lives in
# `scripts/opencode-smoke.sh`/`scripts/opencode_smoke_verify.py`, which this
# just runs on the real target host — the same way `scripts/remote-dev-mode-
# run.sh` only wraps `make dev-mode-smoke`, so nothing is duplicated between
# the local and remote paths.
#
# Deliberately `set -uo pipefail` without `-e`, same reasoning as
# `remote-heart-run.sh`/`remote-dev-mode-run.sh`: a non-zero verdict must
# still be captured into `.exit`, never abort this script before that write
# happens.
set -uo pipefail

cd "$(dirname "$0")/.."

mkdir -p /tmp/ansina-opencode-smoke

{
    git log -1 --format='opencode-smoking %h (%s)'
    make opencode-smoke
} 2>&1 | tee /tmp/ansina-opencode-smoke/run.log

# Written last: scripts/remote-opencode.sh polls for this file's existence, so
# nothing it reads is ever a partial run.
echo "${PIPESTATUS[0]}" >/tmp/ansina-opencode-smoke/.exit
