# Dev Mode & telemetry

`ansina --dev` is a lab/pre-customer posture, never a default: it ships the daemon's
own rotated telemetry files (RSS/tick/decision samples, a redacted log mirror) to an
S3-compatible bucket continuously, by auto-launching and supervising
[Vector](https://vector.dev) as a sidecar.

## The producer: `[telemetry]`

Independent of `--dev` and of the Heart itself: `[telemetry] enabled = true` makes
the daemon append one RSS/tick/decision sample every
`sample_interval_seconds` (default 60s) to a rotated `samples.jsonl`, and mirror its
own redacted log stream to a rotated `log.jsonl` — both under `[telemetry] spool_dir`,
on disk only. `enabled = false` (the default) is a byte-for-byte no-op: no spool
directory, no handler, no sampling loop. See `ansina.example.toml`'s `[telemetry]`
block for every knob (rotation size, spool budget, retention).

## The shipper: Vector under `--dev`

```bash
curl -L https://sh.vector.dev | VECTOR_VERSION=0.45.0 bash -s -- -y
ANSINA_TELEMETRY__ENABLED=true uv run python -m ansina --dev
```

**Install Vector 0.45.x, not latest** — every later release defaults to a request-
checksum behavior Cloudflare R2 rejects outright
([vectordotdev/vector#23029](https://github.com/vectordotdev/vector/issues/23029),
unfixed as of this writing).

`--dev` preflights four things before spawning Vector, cheapest first: `[telemetry]`
itself enabled, `[telemetry.s3]` fully configured, `vector` resolving on `PATH`, and
`deploy/vector.toml` passing `vector validate`. Any one missing is a logged skip,
never a crash — the daemon always boots regardless. Once spawned, Vector tails
`[telemetry]`'s rotated files and uploads them to the same `[telemetry.s3]` bucket
the report-bucket pipeline uses (see `docs/heart/bench.md`), under
`kind=telemetry/`/`kind=runlog/`.

## Verifying it

```bash
make vector-validate      # deploy/vector.toml against the real vector binary, no bucket needed
make dev-mode-smoke       # full loop: boot --dev, wait for samples, verify objects land in the bucket
make remote-dev-mode      # [Mac Mini] run the same smoke check on real target hardware
```

`dev-mode-smoke` boots `ansina --dev` against a scratch database/spool dir with
whatever `[telemetry.s3]` credentials this host already has, then reads the uploaded
objects back out of the bucket directly (never trusting Vector's own exit code or
logs) — under an isolated, easily-deleted `_devmode-smoke/...` key prefix so it never
touches the real corpus. It needs no GPU and no Heart, so it runs the same way on a
plain Linux box as on the Mac Mini.

## Config reference

`ansina.example.toml`'s `[telemetry]`, `[telemetry.s3]`, and `[dev]` blocks document
every setting inline. `[dev] enabled = false` (the default, set only by `--dev`) is
the same byte-for-byte-no-op shape as `[telemetry]`/`[heart]`.
