.PHONY: lint fmt test test-unit test-integration check

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

test:
	uv run pytest tests/ -x -q

test-unit:
	uv run pytest tests/ -x -q --ignore=tests/integration

test-integration:
	uv run pytest tests/integration/ -x -q

check: lint test-unit
