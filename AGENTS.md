# AGENTS.md

Repository guide for coding agents working in `MoreAi`.

## Scope

- This is a Python 3.12 FastAPI service that exposes an OpenAI-compatible API.
- The runtime core is browser automation with Playwright against web chat providers.
- Main source lives under `app/`.
- Tests live under `tests/` and use `pytest`.
- Dependency and tool configuration lives in `pyproject.toml`.
- Common developer entrypoints live in `Makefile`.

## Rule Files

- No Cursor rules were found in `.cursor/rules/`.
- No `.cursorrules` file was found.
- No Copilot instructions file was found at `.github/copilot-instructions.md`.
- If any of those files are added later, treat them as higher-priority instructions and update this file.

## Environment

- Required Python version: `>=3.12`.
- Preferred package manager: `uv`.
- Browser dependency: Playwright Chromium.
- Config is loaded from `.env` via `pydantic-settings`.

## Install Commands

- Base install from `Makefile`: `make install`
- Equivalent commands: `uv sync` and `playwright install chromium`
- Dev tools declared in `pyproject.toml` optional deps: `pytest`, `pytest-asyncio`, `pytest-cov`, `respx`, `ruff`, `mypy`.
- If `ruff` or `mypy` are missing locally, prefer `uv sync --extra dev` or `pip install -e ".[dev]"`.

## Run Commands

- Dev server: `make run`
- Direct dev server: `uvicorn app.main:app --reload --port 8000`
- Production-style local run: `make run-prod`
- Direct production-style run: `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4`
- Recon script: `make recon` or `python scripts/recon_chat_ui.py`
- Google auth bootstrap: `python scripts/bootstrap_google_auth.py --model kimi`

## Test Commands

- Full test suite: `make test`
- Direct full test suite: `uv run pytest -v`
- If `uv run` is not needed in the current shell, `pytest -v` also matches the repo config.
- Tests are configured in `pyproject.toml` with `testpaths = ["tests"]`.

## Single Test Commands

- Single file: `uv run pytest -v tests/test_config.py`
- Single class: `uv run pytest -v tests/test_config.py::TestSettings`
- Single test: `uv run pytest -v tests/test_config.py::TestSettings::test_default_settings`
- Filter by name: `uv run pytest -v -k default_settings`
- Stop on first failure: `uv run pytest -v -x`
- Show print/log output: `uv run pytest -v -s`
- Async tests are handled by `pytest-asyncio` with `asyncio_mode = auto`.

## Lint And Typecheck Commands

- Lint: `make lint`
- Direct lint: `uv run ruff check app tests`
- Typecheck: `make typecheck`
- Direct typecheck: `uv run mypy app`
- If those tools are unavailable, install dev extras first.

## CI Notes

- GitHub Actions currently build and push a Docker image on pushes to `main`.
- Workflow file: `.github/workflows/docker.yml`

## Project Map

- `app/main.py`: FastAPI app setup and lifespan.
- `app/api/routes_openai.py`: API routes.
- `app/services/chat_proxy_service.py`: request orchestration.
- `app/browser/providers/*.py`: provider-specific Playwright logic.
- `app/core/{config,errors,logging}.py`: settings, exceptions, and logging.
- `app/schemas/openai.py`: request/response models.
- `app/pipeline/`: Chain-of-Providers pipeline orchestration subsystem.
  - `app/pipeline/types.py`: Core types (PipelineDefinition, PipelineStage, PipelineContext, StageResult, PipelineTrace, PipelineRegistry).
  - `app/pipeline/builtin_pipelines.py`: Built-in pipeline templates (generate-review-refine, generate-critique-regenerate, draft-verify-finalize).
  - `app/pipeline/executor.py`: PipelineExecutor — sequential stage orchestration with guardrails.
  - `app/pipeline/prompt_builder.py`: Controlled prompt handoff between stages.
  - `app/pipeline/diagnostics.py`: Execution trace storage and querying.
- `app/intelligence/`: Model/provider intelligence for pipeline stage selection.
  - `app/intelligence/types.py`: Core types (ModelRuntimeStats, StageSuitability, CapabilityTag, SelectionPolicy, CandidateRanking, SelectionTrace).
  - `app/intelligence/stats.py`: StatsAggregator — combines analytics, health, and circuit breaker data.
  - `app/intelligence/suitability.py`: SuitabilityScorer — stage-specific suitability scoring with proxy metrics.
  - `app/intelligence/tags.py`: CapabilityRegistry — semantic tags (fast, stable, review_strong, etc.).
  - `app/intelligence/selection.py`: ModelSelector — candidate ranking, selection, and bounded fallback.
- `app/pipeline/observability/`: Pipeline observability and admin control plane.
  - `app/pipeline/observability/trace_model.py`: Enhanced trace models (PipelineExecutionSummary, StageExecutionSummary, StageSelectionExplain, FailureAnalysis, CandidateExplain).
  - `app/pipeline/observability/store.py`: PipelineExecutionStore — bounded recent execution history with filtering.
  - `app/pipeline/observability/recorder.py`: ObservabilityRecorder — converts raw traces to bounded summaries with explainability.

## Formatting

- Ruff line length is `100`.
- Target Python version is `3.12`.
- Use 4-space indentation.
- Do not reformat unrelated code while touching a file.
- Keep changes small and local.
- Prefer straightforward code over abstraction.

## Imports

- Use standard library imports first, then third-party, then `app.*` imports.
- Keep `from app...` imports absolute; that is the dominant project pattern.
- Prefer explicit imports over wildcard imports.
- Avoid local imports unless they prevent a real cycle or meaningfully reduce startup coupling.

## Types

- Use Python 3.12 typing syntax already present in the repo: `str | None`, `list[str]`, `dict[str, Any]`.
- Add type hints for new public functions and methods.
- Match existing pragmatism: mypy is enabled but not strict.
- Prefer `T | None` in new code; keep `Optional[...]` only when matching nearby existing code.
- For Pydantic models, use `Field(...)` for validation constraints and defaults that benefit from clarity.

## Naming

- Functions and variables: `snake_case`.
- Classes: `PascalCase`.
- Constants and config keys: `UPPER_SNAKE_CASE` for env vars, lower-case attributes in settings classes.
- Test classes use `Test...` names.
- Test functions use `test_...` names with behavior-oriented wording.
- Provider classes end with `Provider`.
- Error classes end with `Error`.

## FastAPI And API Patterns

- Keep route handlers thin.
- Validate request shape with Pydantic models in `app/schemas/openai.py`.
- Push orchestration into services rather than growing route handlers.
- Raise `APIError` subclasses for expected API failures.
- Keep `/health`, `/v1/models`, and `/v1/chat/completions` behavior consistent unless the task requires API changes.

## Error Handling

- Use domain-specific exceptions from `app/core/errors.py`.
- Prefer `BadRequestError`, `InternalError`, `BrowserError`, and related subclasses over raw generic exceptions for expected cases.
- Include `details=` when it adds debugging value.
- Preserve existing behavior where `APIError` is re-raised and unexpected exceptions are logged then wrapped.

## Logging

- Use `get_logger(__name__)`.
- Prefer structured logging fields instead of string interpolation.
- Include stable fields such as `request_id`, `model`, `provider_id`, `path`, `timeout`, or `error` when useful.
- Use `logger.exception(...)` for unexpected exceptions when a traceback is valuable.
- Keep log messages concise and operational.

## Browser Automation Guidelines

- Put provider-specific selectors and flow logic in the provider class.
- Favor accessibility locators first, then stable CSS fallback selectors.
- Treat DOM churn and timeouts as normal; code defensively.
- When authentication is provider-specific, integrate with `app/browser/auth.py` and provider hooks such as `detect_login_required()`.

## Configuration Guidelines

- Add new settings in `app/core/config.py` with sensible defaults and validation bounds.
- Follow the existing nested settings pattern for provider-specific config.
- Keep env var naming aligned with existing prefixes like `QWEN_`, `GLM_`, `CHATGPT_`, `YANDEX_`, `KIMI_`, and `GOOGLE_AUTH_`.
- If a new setting matters to users, also update `.env.example` and `README.md`.

## Testing Guidelines

- Add or update tests for behavior changes.
- Prefer focused unit tests under `tests/`.
- Follow the existing class-based pytest organization when editing nearby tests.
- Use `pytest.raises(...)` for exception assertions.
- For HTTP routes, use `fastapi.testclient.TestClient`.
- Mock service boundaries instead of driving real browsers unless the task explicitly requires browser-level testing.

## Pipeline Orchestration Guidelines

- Pipelines are data-driven: define stages declaratively in `PipelineDefinition`.
- Use `PipelineExecutor` for sequential stage execution — do not bypass the executor.
- Each stage receives only what its `InputMapping` explicitly allows (controlled handoff).
- Prompt templates use `{original_request}`, `{previous_output}`, `{draft_output}`, `{review_output}`, `{critique_notes}`, `{verify_output}` variables.
- Pipelines are exposed as OpenAI-compatible model IDs: `pipeline/<pipeline_id>`.
- Guardrails are enforced at two levels:
  - Pydantic: max 3 stages in `PipelineDefinition.stages`.
  - Executor: secondary validation for stage count, no nested pipelines, total timeout.
- Failure policies per stage: `fail_all` (default), `skip`, `fallback`.
- Pipeline traces are stored in `pipeline_diagnostics` and accessible via admin API.
- New pipelines should be added to `app/pipeline/builtin_pipelines.py` and registered via `register_builtin_pipelines()`.
- Config settings use `PIPELINE_` env var prefix.

## Model Intelligence Guidelines

- Pipeline stages can use `selection_policy` for data-driven model selection instead of hard-coded `target_model`.
- SelectionPolicy fields: `preferred_models`, `preferred_tags`, `avoid_tags`, `min_availability`, `max_latency_s`, `avoid_same_model_as_previous`, `fallback_mode`, `allowed_transports`, `excluded_models`, `max_fallback_attempts`.
- Candidate ranking uses weighted composite score: availability (25%) + latency (15%) + stability (15%) + stage_suitability (30%) + tag_bonus (10%) + admin_bonus (5%).
- Availability score = success_rate * 0.5 + health_score * 0.3 + circuit_penalty * 0.2.
- Stage suitability uses role-specific weights (e.g., review role emphasizes stability + reasoning tags).
- Capability tags are semantic labels: `fast`, `stable`, `creative`, `review_strong`, `reasoning_strong`, `cheap`, `experimental`, `browser_only`, `api_preferred`, `long_context`, `code_strong`, `multilingual`.
- Builtin tag assignments live in `app/intelligence/tags.py`.
- Fallback is bounded: max `max_fallback_attempts` (default 2) per stage.
- Selection traces are stored in pipeline context metadata for full traceability.
- If ranking data is insufficient (no analytics history), defaults to reasonable values (0.5 stability, 1.0 availability/latency).
- Admin diagnostics at `/admin/intelligence/models`, `/admin/intelligence/tags`, `/admin/intelligence/ranking/{role}`.

## Pipeline Observability Guidelines

- Pipeline executions are recorded as bounded `PipelineExecutionSummary` (not raw traces) to control memory.
- Execution summaries are stored in `PipelineExecutionStore` (default: 100 max global, 30 per pipeline).
- Stage summaries include bounded input/output summaries (500 chars max), selection explainability, budget info.
- Selection explainability records: candidates considered, excluded (with reasons), viable count, fallback chain.
- Failure analysis classifies root causes: `timeout`, `circuit_breaker`, `no_viable_candidates`, `model_unavailable`, `selection_failed`, `execution_error`.
- Budget tracking: each stage shows `budget_remaining_ms`, pipeline shows `budget_consumed_pct`.
- Admin UI: Pipelines tab at `/admin` shows overview cards, definitions table, recent executions table, trace modal with stage timeline.
- Admin API endpoints:
  - `GET /admin/pipelines/executions?pipeline_id=&status=&limit=` — filtered execution list
  - `GET /admin/pipelines/executions/{execution_id}` — detailed trace with failure analysis
  - `GET /admin/pipelines/executions/store/stats` — store statistics
  - `POST /admin/pipelines/{pipeline_id}/run-test` — diagnostic test execution
  - `POST /admin/pipelines/{pipeline_id}/run-sandbox` — dry-run selection without provider calls
  - `GET /admin/pipelines/stage-performance` — per-model stage performance stats
  - `GET /admin/pipelines/stage-performance/trends` — top performers, cold-start models, fallback-heavy models
  - `GET /admin/pipelines/stage-scoring?stage_role=generate` — full scoring breakdown per model
  - `GET /admin/pipelines/executions/persistent` — executions from SQLite persistent store
- Metrics: `moreai_pipeline_partial_total`, `moreai_pipeline_stage_fallback_total`, `moreai_pipeline_rank_fallback_reason_total`.
- Logs include: `pipeline_observability_recorded` with execution_id, status, duration, stages_completed, fallbacks.
- Do NOT store full raw stage outputs — use bounded summaries only.
- Trace modal in admin UI shows: stage timeline, model/provider per stage, duration, fallback/retry badges, budget remaining, failure analysis panel.
- **Adaptive fallback**: On stage failure, the executor classifies the failure reason (`timeout`, `circuit_breaker`, `service_unavailable`, `model_not_found`, `provider_internal_error`, `execution_error`), computes a bounded score penalty, and re-ranks remaining candidates before picking the next best.
- **Dynamic suitability**: Stage performance data (rolling success rate, fallback rate) is blended with static priors. Cold-start models (< 5 samples) use mostly static scoring (90% static, 10% dynamic). Full data (100+ samples) shifts to 70% dynamic influence.
- **Scoring formula**: `final = base_static + dynamic_adjustment - failure_penalty`, where base_static uses role-weighted availability/latency/stability/tag_bonus, dynamic is blended from rolling performance data, and penalty is failure-type-specific (0.10-0.30).
- **Traceability**: Every candidate ranking includes `scoring_breakdown` with base_static_score, dynamic_adjustment, failure_penalty, performance data (success_rate, fallback_rate, sample_count, data_confidence).

## When Editing

- Preserve public API contracts unless the task explicitly changes them.
- Avoid renaming env vars, models, or routes casually.
- Avoid introducing new dependencies unless clearly justified.
- Prefer extending existing files before creating new modules for small features.

## Pre-Completion Checklist

- Run relevant tests for touched code.
- Run `uv run ruff check app tests` if Ruff is installed.
- Run `uv run mypy app` when type-sensitive code changed and mypy is installed.
- Mention any commands you could not run because local dev tools were unavailable.
