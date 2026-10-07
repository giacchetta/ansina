SHELL := /bin/bash
.DEFAULT_GOAL := help

UV_INSTALL_DIR := $(HOME)/.local/bin
UV := $(shell command -v uv 2>/dev/null || echo "$(UV_INSTALL_DIR)/uv")

.PHONY: help
help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

.PHONY: uv
uv: ## Install uv (Astral installer) if not already on PATH — macOS and Linux
	@if command -v uv >/dev/null 2>&1 || [ -x "$(UV)" ]; then \
		echo "uv already installed: $$(command -v uv || echo $(UV))"; \
	else \
		echo "uv not found, installing via astral.sh..."; \
		curl -LsSf https://astral.sh/uv/install.sh | sh; \
		echo "Installed to $(UV_INSTALL_DIR). 'make' targets in this session already work (they call uv by full path)."; \
		echo "For 'uv' to work directly in your shell, open a new terminal or source your shell's rc file (e.g. 'source ~/.zshrc' / 'source ~/.bashrc')."; \
	fi

.PHONY: sync
sync: uv ## Install/sync project dependencies into .venv
	$(UV) sync

.PHONY: lint
lint: ## Lint with ruff
	$(UV) run ruff check .

.PHONY: format
format: ## Auto-format code with ruff
	$(UV) run ruff format .

.PHONY: format-check
format-check: ## Check formatting without modifying files
	$(UV) run ruff format --check .

.PHONY: typecheck
typecheck: ## Run mypy in strict mode (config-driven: src + tests, see pyproject.toml)
	$(UV) run mypy

.PHONY: test
test: ## Run the full test suite (unit + e2e)
	$(UV) run pytest

.PHONY: test-unit
test-unit: ## Run only the unit test suite (100% coverage enforced — for a single-file run, call `uv run pytest <path> --no-cov` directly)
	$(UV) run pytest tests/unit

.PHONY: test-e2e
test-e2e: ## Run only the e2e (black-box subprocess) test suite
	$(UV) run pytest tests/e2e --no-cov

.PHONY: precommit
precommit: ## Run pre-commit hooks against all files
	$(UV) run pre-commit run --all-files

# Mac-only (issue #53): the Heart bench harness against a real MLX model — no MLX
# adapter is viable on either CI leg, so this never runs there and is deliberately
# left out of `check`/`check-all` below, the same way `tui-*` is scoped to `tui/`'s
# own project. `ARGS` passes flags through, e.g.
# `make heart-bench ARGS='--model-repo mlx-community/Qwen3.5-4B-MLX-4bit'`.
.PHONY: heart-bench
heart-bench: ## [Mac only] Bench a real MLX model against the tick fixture set (uv sync --extra mlx required)
	$(UV) run --extra mlx python -m ansina.heart.eval $(ARGS)

.PHONY: heart-bench-sync
heart-bench-sync: uv ## [Mac only] Sync dependencies including the mlx extra (used by scripts/remote-heart-run.sh)
	$(UV) sync --extra mlx

# Issue #58: `make remote-heart` is the agent's entire surface for a real-hardware
# bench run — every ssh/pipe/detached-spawn command lives inside the two scripts
# below, never typed ad hoc as an ssh command string. Host/path come from .envrc
# (gitignored); see README.md's "Real-hardware bench" section.
.PHONY: remote-heart
remote-heart: ## [Mac Mini] Bench on real hardware and copy the reports back (see .envrc)
	scripts/remote-heart.sh run $(ARGS)

.PHONY: remote-heart-tail
remote-heart-tail: ## Tail the current/last remote bench run's log
	scripts/remote-heart.sh tail

.PHONY: remote-heart-attach
remote-heart-attach: ## Attach to the live bench tmux session on the Mac Mini (interactive)
	scripts/remote-heart.sh attach

# Issue #55's Mac Mini acceptance check: unlike `remote-heart` above (the eval
# harness), this boots the real daemon with the Heart/tick loop enabled against a
# scratch database, waits for a few real ticks, fetches `GET /heart/journal`, and
# verifies every journal row matches what the daemon's own log line reported for
# that tick. Same committed-script-behind-one-target pattern as `remote-heart`, its
# own tmux session so the two never collide, and the same `.envrc` host/path.
.PHONY: remote-heart-journal-smoke
remote-heart-journal-smoke: ## [Mac Mini] Boot the daemon for real and verify heart_journal against its own log (see .envrc)
	scripts/heart-journal-smoke.sh run $(ARGS)

.PHONY: remote-heart-journal-smoke-tail
remote-heart-journal-smoke-tail: ## Tail the current/last journal-smoke run's log
	scripts/heart-journal-smoke.sh tail

.PHONY: remote-heart-journal-smoke-attach
remote-heart-journal-smoke-attach: ## Attach to the live journal-smoke tmux session on the Mac Mini (interactive)
	scripts/heart-journal-smoke.sh attach

# Issue #56's multi-hour heartbeat soak: unlike `remote-heart`/`remote-heart-journal-
# smoke` above (both short, blocking calls), a soak runs for hours — `start` launches
# it in its own tmux session (`ansina-soak`, so it never collides with the other two)
# and returns immediately rather than polling inside `make`; `status`/`fetch` are
# separate, later invocations that can run from a different local session entirely.
# Mac-only (no MLX adapter is viable on either CI leg), so — like `heart-bench` and
# the two targets above — deliberately left out of `check`/`check-all` below.
.PHONY: remote-heart-soak-start
remote-heart-soak-start: ## [Mac Mini] Start a multi-hour soak in the background (see .envrc, docs/heart/soak.md)
	scripts/heart-soak.sh start $(ARGS)

.PHONY: remote-heart-soak-status
remote-heart-soak-status: ## Is the soak still running, and how far in?
	scripts/heart-soak.sh status $(ARGS)

.PHONY: remote-heart-soak-fetch
remote-heart-soak-fetch: ## Once finished: fetch the soak's raw data and render docs/heart/soak/soak-<date>.md
	scripts/heart-soak.sh fetch $(ARGS)

.PHONY: remote-heart-soak-tail
remote-heart-soak-tail: ## Tail the live/last soak run's log
	scripts/heart-soak.sh tail $(ARGS)

.PHONY: remote-heart-soak-attach
remote-heart-soak-attach: ## Attach to the live soak tmux session on the Mac Mini (interactive)
	scripts/heart-soak.sh attach

.PHONY: remote-heart-soak-stop
remote-heart-soak-stop: ## End a running soak early
	scripts/heart-soak.sh stop

# Issue #59: uploads every local bench/soak report not already in the configured
# [telemetry.s3] bucket -- running it once is the backlog migration. Not Mac-only
# (unlike heart-bench/remote-heart, it just mirrors whatever local files exist, no
# MLX adapter needed) but, like those, performs real network writes, so it's
# deliberately left out of check/check-all below.
.PHONY: heart-bench-publish
heart-bench-publish: ## Upload every local bench/soak report not already in the report bucket (see [telemetry.s3])
	$(UV) run --extra s3 python -m ansina.heart.eval.publish $(ARGS)

# Issue #62: the Vector sidecar's own config. Not Mac-only (Dev Mode needs no
# GPU/Heart — see `src/ansina/dev/`), but Vector is installed on neither CI leg,
# so this stays out of `check`/`check-all` the same way `heart-bench` does.
.PHONY: vector-validate
vector-validate: ## Validate deploy/vector.toml (requires the `vector` binary on PATH)
	scripts/vector-validate.sh

# `make dev-mode-smoke` is the primary end-to-end verification for issue #62: boots
# `ansina --dev` against a scratch database/spool dir with whatever real
# [telemetry.s3] credentials this host already has (.envrc/ansina.toml), and
# verifies objects actually land in the bucket by reading them back out of it.
# Runs unchanged here and on the Mac Mini — `remote-dev-mode` below just runs this
# same target over there. Requires `vector` on PATH and real bucket credentials, so
# it stays out of `check`/`check-all`, the same reasoning as `heart-bench-publish`.
.PHONY: dev-mode-smoke
dev-mode-smoke: ## Boot `ansina --dev` against a scratch spool + real bucket creds and verify objects land (requires vector + [telemetry.s3] creds)
	scripts/dev-mode-smoke.sh

.PHONY: remote-dev-mode
remote-dev-mode: ## [Mac Mini] Run the dev-mode smoke check on real target hardware (see .envrc)
	scripts/remote-dev-mode.sh run $(ARGS)

.PHONY: remote-dev-mode-tail
remote-dev-mode-tail: ## Tail the current/last remote dev-mode run's log
	scripts/remote-dev-mode.sh tail

.PHONY: remote-dev-mode-attach
remote-dev-mode-attach: ## Attach to the live dev-mode tmux session on the Mac Mini (interactive)
	scripts/remote-dev-mode.sh attach

.PHONY: check
check: lint format-check typecheck test ## Run everything the daemon's CI `check` job runs

# `tui/` is a standalone project (issue #31): own pyproject.toml, own uv.lock, own
# .venv, own CI job. `make check` stays daemon-only above so it keeps mirroring the
# `check` CI job exactly — these targets are the `tui` CI job's local mirror instead.
# `--directory tui` (not `--project tui`) throughout: it actually changes the
# subprocess's cwd, which matters for pytest's `--cov=src/ansina_tui` in
# `tui/pyproject.toml` — that path is resolved relative to cwd, not to the
# pyproject.toml that declared it.
.PHONY: tui-sync
tui-sync: uv ## Install/sync tui/'s dependencies into tui/.venv
	$(UV) sync --directory tui

.PHONY: tui-lint
tui-lint: ## Lint tui/ with ruff
	$(UV) run --directory tui ruff check .

.PHONY: tui-format
tui-format: ## Auto-format tui/ with ruff
	$(UV) run --directory tui ruff format .

.PHONY: tui-format-check
tui-format-check: ## Check tui/'s formatting without modifying files
	$(UV) run --directory tui ruff format --check .

.PHONY: tui-typecheck
tui-typecheck: ## Run mypy in strict mode on tui/ (config-driven, see tui/pyproject.toml)
	$(UV) run --directory tui mypy

.PHONY: tui-test
tui-test: ## Run tui/'s test suite (100% coverage enforced)
	$(UV) run --directory tui pytest

.PHONY: tui-check
tui-check: tui-lint tui-format-check tui-typecheck tui-test ## Run everything the tui CI job runs

.PHONY: check-all
check-all: check tui-check ## Run both the daemon's and tui/'s full check suites

.PHONY: clean
clean: ## Remove caches, build artifacts, and the virtualenv
	rm -rf .venv .ruff_cache .mypy_cache .pytest_cache htmlcov .coverage dist build src/*.egg-info
	rm -rf tui/.venv tui/.ruff_cache tui/.mypy_cache tui/.pytest_cache tui/htmlcov tui/.coverage tui/dist tui/build
