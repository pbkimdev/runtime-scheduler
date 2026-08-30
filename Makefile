.PHONY: dev test lint fmt build verify allocate-dry contract

dev:
	uv sync

test:
	uv run pytest -q

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff format .
	uv run ruff check --fix .

build:
	uv build

verify: lint test

allocate-dry:
	uv run scheduler allocate --dry-run

contract:
	uv run scheduler contract-check
