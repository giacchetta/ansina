# Heart bench driver & report bucket

How to run the Heart's decision-accuracy bench against a real MLX model, and where
the resulting reports end up. A run's own output is a report pair (`.md`/`.json`)
under `docs/heart/bench/` (gitignored — see below), not this file.

## What this is not

The bench harness boots no daemon at all — it loads a model directly via
`ansina.heart.eval` and calls `generate()` against a fixture set. For the real
daemon's own tick loop against the same validated model, see
`docs/heart/mac-mini-m4.toml`.

## Running a bench

MLX only runs on Apple Silicon, so there are two ways to run one:

```bash
make heart-bench                          # [Mac only] run locally
make remote-heart                         # [any machine] run on a remote Mac over ssh/tmux
```

`remote-heart` needs `.envrc` (gitignored) with the same two variables
`scripts/heart-soak.sh`/`scripts/heart-journal-smoke.sh` also require:

```bash
export ANSINA_REMOTE_HOST=<user@host>
export ANSINA_REMOTE_PATH=<path to the ansina checkout on that host>
```

It force-syncs that checkout to your current branch's **pushed** HEAD (push first —
an unpushed commit aborts the run), runs the bench in its own tmux session
(`ansina-bench`), and copies the report pair back, auto-suffixing (`-2`, `-3`, …)
instead of ever overwriting one:

```bash
make remote-heart-tail                    # tail the live log
make remote-heart-attach                  # attach to the tmux session interactively
```

## Two suites

```bash
make remote-heart ARGS='--suite triage'   # bench the request-triage experiment instead
```

`--suite {tick,triage}` (default `tick`) picks which fixture set and gate runs.
`triage` benches the gated request-triage experiment (`ansina.heart.triage`) — wired
into no route or the tick loop, bench-only. Both suites share this harness, the
`remote-heart` driver, and the report bucket's `kind=bench/` prefix below,
distinguished there by a `suite` field.

## A related, separate check

```bash
make remote-heart-journal-smoke           # [Mac Mini] boot the real daemon, verify heart_journal
```

Unlike the bench above, this boots the real daemon (Heart + tick loop enabled)
against a scratch database, waits for a few real ticks, and verifies every
`GET /heart/journal` row matches its own `"heart tick completed"` log line. Its own
tmux session (`ansina-journal-smoke`) never collides with a bench run; no report is
written to `docs/` — the verdict prints to the terminal.

## Where reports go

`docs/heart/bench/` is gitignored, not committed — a report's raw per-fixture model
output has no size ceiling, and auto-suffixing means the corpus only ever grows,
which belongs in an object store, not git history.

```bash
make heart-bench-publish                  # upload every local report not already in the bucket
```

Set the bucket/endpoint/region in `ansina.toml` and the two credentials in `.envrc`
(never `ansina.toml` — every Ansina secret is env-only):

```toml
# ansina.toml
[telemetry.s3]
enabled = true
bucket = "ansina-heart-corpus"
endpoint_url = "https://<account-id>.r2.cloudflarestorage.com"  # Cloudflare R2 — verified; any S3-compatible endpoint works
region = "auto"
```

```bash
# .envrc (gitignored)
export ANSINA_TELEMETRY__S3__ACCESS_KEY_ID=<access key id>
export ANSINA_TELEMETRY__S3__SECRET_ACCESS_KEY=<secret access key>
```

With that in place, `make heart-bench`/`make remote-heart` upload the report pair
they just wrote automatically (auto-suffixing the *key* on a same-day collision);
`make heart-bench-publish` is the separate, one-shot backlog migration for anything
not already uploaded — a verified no-op on a second run. With `[telemetry.s3]` left
disabled (the default), reports stay local-only and nothing changes about how the
bench itself runs or exits. `python -m ansina.heart.eval.publish --dry-run` prints
every key it would upload with no bucket configured at all.

Every uploaded key answers to one versioned schema,
[`docs/ml/corpus-contract.md`](../ml/corpus-contract.md) — Ansina publishes it and
reads nothing back; a future change to the contract arrives as a GitHub issue filed
against this repo, not the reverse.
