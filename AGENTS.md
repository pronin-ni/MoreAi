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
