#!/usr/bin/env bash
# `make opencode-install` — installs OpenCode (anomalyco/opencode) via the
# official v2 installer, for M9's "Hands" (issue #74). Requires an explicit
# --version/-v (or --binary/-b to install from a local binary instead) — the
# official installer's own flag spelling, forwarded as-is, never translated
# into a different interface. This script deliberately refuses to install
# "latest" silently: no OpenCode release has been validated against Ansina
# yet (`[opencode] required_version_prefix` defaults to "", meaning "nothing
# pinned"), so an unreviewed upgrade must never happen by accident the way it
# would if this script picked a default version itself.
#
# Verified empirically against the real v2 install script and the project's
# own GitHub issue tracker — not assumed:
#   - The v2 endpoint is https://opencode.ai/v2/install, not the bare
#     /install (that one redirects to the v1 branch's own installer — a
#     materially different CLI, including a different `--version` output
#     shape: v1 prints a bare "1.19.3", v2 prints "opencode v2.1.1"; see
#     `OpencodeSettings.required_version_prefix`'s own docstring).
#   - The downloaded artifact is a single self-contained binary built via
#     `bun build --compile`, which bundles the Bun runtime *inside* the
#     executable. No separate Bun/Node install is needed on the target
#     machine, despite OpenCode itself being written in TypeScript — the
#     binary this script installs needs nothing else on PATH to run.
#   - --version/-v (or a VERSION env var the installer reads directly) pins
#     a release; the installer strips a leading "v" itself, so both "2.1.1"
#     and "v2.1.1" work identically.
#
# Mac/Linux/Windows(-via-a-shell) all supported by the upstream installer
# itself; this script makes no platform assumption beyond what `curl -fsSL
# ... | bash` already requires. Never run by CI (neither leg installs
# `opencode` — the same "not found on PATH" skip path `ansina.opencode.
# preflight()` exercises for real on both), the same reasoning `make
# vector-validate` documents for Vector.
set -euo pipefail

has_pin=false
has_help=false
for arg in "$@"; do
    case "$arg" in
    --version | -v | --binary | -b) has_pin=true ;;
    --help | -h) has_help=true ;;
    esac
done

if [[ "$has_help" == true ]]; then
    curl -fsSL https://opencode.ai/v2/install | bash -s -- "$@"
    exit 0
fi

if [[ "$has_pin" != true ]]; then
    echo "usage: make opencode-install ARGS='--version <version>'" >&2
    echo "   or: make opencode-install ARGS='--binary <path>'" >&2
    echo >&2
    echo "e.g. ARGS='--version 2.1.1' — no release is pinned/tested against" >&2
    echo "Ansina yet, so this script refuses to install \"latest\" silently." >&2
    exit 2
fi

curl -fsSL https://opencode.ai/v2/install | bash -s -- "$@"

echo >&2
if command -v opencode >/dev/null 2>&1; then
    echo "Installed. Real 'opencode --version' output (copy into" >&2
    echo "ANSINA_OPENCODE__REQUIRED_VERSION_PREFIX once validated against a" >&2
    echo "real preflight/run):" >&2
    opencode --version
else
    echo "Installed, but 'opencode' is not yet on this shell's PATH — open a" >&2
    echo "new shell, or check wherever the installer just placed it." >&2
fi
