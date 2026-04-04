.PHONY: install run test lint typecheck clean docker-build docker-up docker-down recon

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
	rm -rf .pytest_cache/ .mypy_cache/ htmlcov/ .coverage
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
