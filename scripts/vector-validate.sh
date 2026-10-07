#!/usr/bin/env bash
# `make vector-validate` — validates deploy/vector.toml's syntax/schema with no
# network call and no real credential required. Runs with real `${VAR}`
# interpolation (so a typo'd variable name in the config is caught, unlike
# `--no-environment`) and `--skip-healthchecks` (so this never tries to actually
# reach a bucket) — the exact call `ansina.dev.vector.preflight()` makes at daemon
# boot. See issue #62.
#
# Requires the `vector` binary on PATH, **pinned to 0.45.x** — not installed by
# this script. See `ansina.dev.vector`'s own module-level comment for why: every
# later release's `aws_s3` sink is incompatible with Cloudflare R2
# (vectordotdev/vector#23029, unfixed as of this writing). Install a matching
# release, e.g.:
#   curl -L https://sh.vector.dev | VECTOR_VERSION=0.45.0 bash -s -- -y
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v vector >/dev/null 2>&1; then
    echo "vector is not on PATH — install 0.45.x first, e.g.:" >&2
    echo "  curl -L https://sh.vector.dev | VECTOR_VERSION=0.45.0 bash -s -- -y" >&2
    exit 2
fi

SCRATCH="${TMPDIR:-/tmp}/ansina-vector-validate"
rm -rf "$SCRATCH"
mkdir -p "$SCRATCH/spool" "$SCRATCH/vector-data"

export ANSINA_TELEMETRY_SPOOL_DIR="$SCRATCH/spool"
export ANSINA_TELEMETRY_VECTOR_DATA_DIR="$SCRATCH/vector-data"
export ANSINA_TELEMETRY_S3_BUCKET="placeholder-bucket"
export ANSINA_TELEMETRY_S3_ENDPOINT_URL="https://placeholder.example.com"
export ANSINA_TELEMETRY_S3_REGION="auto"
export ANSINA_TELEMETRY_S3_KEY_PREFIX=""
export AWS_ACCESS_KEY_ID="placeholder"
export AWS_SECRET_ACCESS_KEY="placeholder"

vector validate --skip-healthchecks deploy/vector.toml
