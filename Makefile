SHELL := /bin/bash
.DEFAULT_GOAL := help

UV_INSTALL_DIR := $(HOME)/.local/bin
UV := $(shell command -v uv 2>/dev/null || echo "$(UV_INSTALL_DIR)/uv")

.PHONY: help
help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

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
test-unit: ## Run only the unit test suite
	$(UV) run pytest tests/unit

.PHONY: test-e2e
test-e2e: ## Run only the e2e (black-box subprocess) test suite
	$(UV) run pytest tests/e2e --no-cov

.PHONY: precommit
precommit: ## Run pre-commit hooks against all files
	$(UV) run pre-commit run --all-files

.PHONY: check
check: lint format-check typecheck test ## Run everything CI runs

.PHONY: clean
clean: ## Remove caches, build artifacts, and the virtualenv
	rm -rf .venv .ruff_cache .mypy_cache .pytest_cache htmlcov .coverage dist build src/*.egg-info
