.PHONY: install dev test lint quality-fast quality-integration quality-gate run migrate worker compose-up compose-down

install:
	python -m pip install -e '.[dev]'

dev:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test:
	pytest -q

# Network-free production-boundary checks. The gate forces api_only and
# external storage and uses only fixture/in-process Provider transports.
quality-fast:
	python scripts/quality_gate.py fast

quality-integration:
	python scripts/quality_gate.py integration

quality-gate:
	python scripts/quality_gate.py all

migrate:
	python -m app.cli migrate

lint:
	ruff check app timeline_engine tests scripts

run:
	uvicorn app.main:app --host 0.0.0.0 --port 8000

worker:
	python -m app.cli process-jobs --watch --limit 10 --interval 2

compose-up:
	docker compose -f infra/docker-compose.yml up --build

compose-down:
	docker compose -f infra/docker-compose.yml down
