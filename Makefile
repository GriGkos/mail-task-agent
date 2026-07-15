.PHONY: install test lint format-check migrate run docker-up docker-workers docker-down docker-logs

install:
	python -m pip install -e ".[dev]"

test:
	pytest

lint:
	ruff check .

format-check:
	ruff format --check .

migrate:
	alembic upgrade head

run:
	uvicorn app.main:app --reload

docker-up:
	docker compose up --build

docker-workers:
	docker compose --profile workers up --build

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f api
