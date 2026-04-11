.PHONY: install run test lint typecheck clean docker-build docker-up docker-down recon \
        e2e-install e2e-smoke e2e-full e2e-live e2e-report

install:
	uv sync
	playwright install chromium

run:
	uvicorn app.main:app --reload --port 8000

run-prod:
	uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4

test:
	pytest -v

lint:
	ruff check app/ tests/

typecheck:
	mypy app/

clean:
	rm -rf .pytest_cache/ .mypy_cache/ htmlcov/ .coverage playwright-report/ test-results/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete

docker-build:
	docker-compose build

docker-up:
	docker-compose up -d

docker-down:
	docker-compose down

docker-logs:
	docker-compose logs -f

recon:
	python scripts/recon_chat_ui.py

# ── E2E Regression ──

e2e-install:
	uv run playwright install chromium

e2e-smoke:
	uv run pytest e2e/ -m smoke --headed --tracing=retain-on-failure --screenshot=only-on-failure --html=playwright-report/smoke.html --self-contained-html

e2e-full:
	uv run pytest e2e/ -m "regression and not live" --headed --tracing=retain-on-failure --screenshot=only-on-failure --html=playwright-report/full.html --self-contained-html

e2e-live:
	LIVE=1 uv run pytest e2e/ -m live --headed --tracing=on --screenshot=on --html=playwright-report/live.html --self-contained-html

e2e-report:
	@echo "Open playwright-report/ in a browser to view the HTML report"
