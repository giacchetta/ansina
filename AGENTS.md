# 🛑 SYSTEM DIRECTIVE & EXECUTION SEQUENCE (MANDATORY)

Before reading user requests or modifying ANY file, you MUST follow this exact execution sequence:

1. **STEP 1 — GUARDRAILS CHECK**: Read `.agents/guardrails/*.md`. Any violation results in immediate execution termination.
2. **STEP 2 — SYSTEM CORE RULES**: Read `.agents/core/*.md` to establish non-negotiable coding and quality standards.
3. **STEP 3 — READ LOCAL ARCHITECTURE**: Read Section 2 below (`# Local Architecture Blueprint`). DO NOT execute recursive directory exploration scripts or spawn sub-agents to explore the codebase.
4. **STEP 4 — EXECUTE TASK**: Perform the task adhering strictly to the above.
5. **STEP 5 — ARCHITECTURE PROTOCOL**: Read `.agents/protocols/agents-md-protocol.md`. If your changes modified system boundaries, directory structures, or APIs, you MUST update Section 2 of this `AGENTS.md` file before completing your task.

---

# 🏛️ Local Architecture Blueprint

## Layer 1: Foundations
- **Repository Purpose**: Ansina is a self-owned AI agent exposing a single internal REST API — no chat channels — built around a small always-on "Heart" model (in-process, ≤4B/8k-ctx) and a large remote "Brain" model (35B+) for actual reasoning.
- **Tech Stack**: Python ≥ 3.14 / FastAPI / SQLite. Embedded inference in-process via MLX (primary, Apple Silicon) with a llama-cpp-python fallback. See `docs/architecture/blueprint.md` for the full rationale and the OpenClaw comparison this design departs from.

## Layer 2: Directory Layout
- `Makefile`: dev-workflow entry point — bootstraps `uv` (Astral installer, macOS/Linux) if missing, wraps `sync`/`lint`/`format`/`typecheck`/`test`/`check`/`clean`.
- `src/ansina/`: Core application source code (src-layout package, `py.typed` marker for downstream type-checking).
- `src/ansina/__main__.py`: Process entry point (`python -m ansina` / the `ansina` console script). Loads config, configures logging, builds the app via `create_app()`, hands it to uvicorn.
- `src/ansina/api/`: REST API surface (issue #4). `create_app()` in `api/app.py` is the FastAPI factory. `api/middleware.py` assigns/echoes the request id and feeds `logging`'s correlation-id contextvar. `api/auth.py` (issue #5) enforces a static bearer token via `hmac.compare_digest`, deny-by-default — every route needs the token except `PUBLIC_PATHS` (`/healthz`, `/readyz`); a `None` token (dev mode) disables enforcement, and `config.settings` refuses to boot at all if that's paired with a non-loopback bind. `api/problems.py` + `api/exception_handlers.py` map every error door (`AnsinaError`, HTTP errors, validation errors, unhandled exceptions) to RFC 9457 `application/problem+json`. `api/readiness.py` is a named-check registry backing `GET /readyz`; later milestones register their own checks there instead of editing the route. `api/routes/health.py` holds the only routes M0 ships: `/healthz`, `/readyz`, `/version`.
- `src/ansina/config/`: Typed `Settings` (pydantic-settings). Layering: defaults → `ansina.toml` → `ANSINA_*` env vars. Load via `load_settings()`, never `os.getenv()` directly. Secrets are env-only — a `SecretStr` field set in the TOML file is a startup error.
- `ansina.example.toml`: documents the config file shape; copy to `ansina.toml` (gitignored) to use.
- `src/ansina/errors.py`: `AnsinaError` base exception with a stable, machine-readable `code` (`ClassVar[str]`); every subclass must declare its own `code` or class creation raises. `ansina.config.ConfigError` subclasses it.
- `src/ansina/logging/`: JSON structured logging via `logging.config.dictConfig`. Redaction runs inside `JsonFormatter`, not at call sites — never bypass it by formatting log strings yourself. Request/correlation ids via `contextvars` (`request_id_scope()`). Boot with `configure_logging(load_settings())`, then use `get_logger(__name__)` everywhere else, never bare `logging.getLogger()`.
- `tests/unit/`: Mirrors `src/ansina/` 1:1. `tests/e2e/`: black-box, launches `python -m ansina` as a subprocess.
- `docs/architecture/`: `blueprint.md` — architecture rationale and roadmap.
- `.agents/`: Synced central prompts, guardrails, and protocols.

## Layer 3: Data Flow & Entry Points
- Entry points: `python -m ansina` and the `ansina` console script (`[project.scripts]`), both resolving to `src/ansina/__main__.py:main`.
- Boot sequence: `load_settings()` → `configure_logging(settings)` → `create_app(settings)` (`src/ansina/api/app.py`) → `uvicorn.run(...)`. A `ConfigError` during boot prints the aggregated report to stderr and exits non-zero, never a traceback. `load_settings()` itself refuses to construct `Settings` (issue #5) when `server.host` isn't loopback and no `security.api_token` is configured — that invariant is unbypassable since it lives on `Settings`, not in `create_app`.
- Request flow: `RequestIdMiddleware` (outermost) binds/echoes a request id (feeding `logging`'s correlation-id contextvar) → `BearerAuthMiddleware` (401 `problem+json` if the route isn't in `PUBLIC_PATHS` and the bearer token is missing/wrong) → route → response, or an exception mapped to `application/problem+json` by the handlers in `api/exception_handlers.py`.

## Layer 4: External Integrations
- [List databases, third-party APIs, or external services]

## Layer 5: Domain Blueprint
- **Heart** (≤4B, 8k ctx, in-process): owns the autonomic tick loop only — decide idle / act / escalate. Never answers user requests directly. Every prompt built for it must fit its 8k window; treat that as a hard constraint, not a target.
- **Brain** (35B+, remote via `BrainProvider` port): owns all real reasoning. The Heart never replaces it or bypasses it for substantive answers.
- Three additional Heart duties (request triage, context curation, structured extraction) are **deliberately not implemented**. Prior embedded-model attempts produced low-quality output — each duty is a gated experiment (`Backlog — Experiments` milestone) requiring a benchmark win over a stated baseline before adoption. Do not fold this logic into the tick loop without that gate passing.
- No channel concept anywhere (no Discord/WhatsApp/etc., and no `ChannelId`-shaped abstraction either) — see `docs/architecture/blueprint.md` §2 for why this is refused deliberately, not an oversight.
- Primary deployment target is a Mac Mini (Apple M4, 16 GB unified memory) — adapter and performance decisions prioritize that hardware, not the local dev machine.
