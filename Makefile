.PHONY: install up down test test-fast lint format types check build docs clean

install:
	uv sync --all-packages

up:
	docker compose -f docker/compose.rs.yaml up -d --wait
	docker compose -f docker/compose.sharded.yaml up -d --wait

down:
	docker compose -f docker/compose.rs.yaml down -v
	docker compose -f docker/compose.sharded.yaml down -v

test:
	uv run pytest

test-fast:
	uv run pytest -m "not sharded"

format:
	uv run ruff format packages bench examples
	uv run ruff check packages bench examples --fix

lint:
	uv run ruff format --check packages bench examples
	uv run ruff check packages bench examples

types:
	uv run mypy packages/mongospecs/src/mongospecs packages/mongospecs-web/src/mongospecs_web bench/src/mongospecs_bench

check: lint types test

build:
	uv build --all-packages

docs:
	uv run --group docs zensical build --clean

clean:
	rm -rf dist build site .*_cache .venv-*
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
