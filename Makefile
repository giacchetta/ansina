SHELL := /bin/bash
.DEFAULT_GOAL := help

UV_INSTALL_DIR := $(HOME)/.local/bin
UV := $(shell command -v uv 2>/dev/null || echo "$(UV_INSTALL_DIR)/uv")

.PHONY: help
help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

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
