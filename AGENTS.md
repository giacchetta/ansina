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
- `src/ansina/config/`: Typed `Settings` (pydantic-settings). Layering: defaults → `ansina.toml` → `ANSINA_*` env vars. Load via `load_settings()`, never `os.getenv()` directly. Secrets are env-only — a `SecretStr` field set in the TOML file is a startup error.
- `ansina.example.toml`: documents the config file shape; copy to `ansina.toml` (gitignored) to use.
- `tests/unit/`: Mirrors `src/ansina/` 1:1. `tests/e2e/`: black-box, launches `python -m ansina` as a subprocess.
- `docs/architecture/`: `blueprint.md` — architecture rationale and roadmap.
- `.agents/`: Synced central prompts, guardrails, and protocols.

## Layer 3: Data Flow & Entry Points
- No entry point exists yet. `[project.scripts] ansina` and `python -m ansina` (`src/ansina/__main__.py`) are added by issue #4 (REST API skeleton) — do not assume either works before then.

## Layer 4: External Integrations
- [List databases, third-party APIs, or external services]

## Layer 5: Domain Blueprint
- **Heart** (≤4B, 8k ctx, in-process): owns the autonomic tick loop only — decide idle / act / escalate. Never answers user requests directly. Every prompt built for it must fit its 8k window; treat that as a hard constraint, not a target.
- **Brain** (35B+, remote via `BrainProvider` port): owns all real reasoning. The Heart never replaces it or bypasses it for substantive answers.
- Three additional Heart duties (request triage, context curation, structured extraction) are **deliberately not implemented**. Prior embedded-model attempts produced low-quality output — each duty is a gated experiment (`Backlog — Experiments` milestone) requiring a benchmark win over a stated baseline before adoption. Do not fold this logic into the tick loop without that gate passing.
- No channel concept anywhere (no Discord/WhatsApp/etc., and no `ChannelId`-shaped abstraction either) — see `docs/architecture/blueprint.md` §2 for why this is refused deliberately, not an oversight.
- Primary deployment target is a Mac Mini (Apple M4, 16 GB unified memory) — adapter and performance decisions prioritize that hardware, not the local dev machine.
