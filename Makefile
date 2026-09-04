.PHONY: help build update check fix format lint typecheck test clean

help:
	@echo "Available targets:"
	@echo "  make build        - Set up development environment"
	@echo "  make update       - Bump Python packages, site packages, and pinned actions"
	@echo "  make check        - Run all quality checks (lint, format, typecheck, test)"
	@echo "  make fix          - Auto-fix issues (ruff check --fix, ruff format)"
	@echo "  make format       - Format code with ruff"
	@echo "  make lint         - Lint code with ruff"
	@echo "  make typecheck    - Type check with ty"
	@echo "  make test         - Run pytest"
	@echo "  make clean        - Clean up cache files"
	@echo "  make help         - Show this help message"

build:
	uv sync --group dev

# Three dependency sets live in this repository and no single tool sees all of
# them: uv owns uv.lock (within the ranges in pyproject.toml), pnpm owns
# site/pnpm-lock.yaml, and the commit-pinned actions in .github/workflows are
# plain YAML. `pnpm up -i` is interactive; pinact rewrites each pin to the
# latest release and keeps the version comment beside it.
update:
	uv sync --group dev --upgrade
	cd site && pnpm run update
	@command -v pinact >/dev/null || { echo "pinact is not installed: brew install pinact"; exit 1; }
	pinact run --update

check:
	@echo "\n========================================================"
	@echo "Running lint..."
	@echo "========================================================"
	uv run ruff check .
	@echo "\n========================================================"
	@echo "Running format check..."
	@echo "========================================================"
	uv run ruff format --check .
	@echo "\n========================================================"
	@echo "Running typecheck..."
	@echo "========================================================"
	uv run ty check
	@echo "\n========================================================"
	@echo "Running tests..."
	@echo "========================================================"
	uv run pytest
	@echo "\n========================================================"
	@echo "✅ All checks passed!"
	@echo "========================================================"

format:
	uv run ruff format .

fix:
	uv run ruff check . --fix
	uv run ruff format .

lint:
	uv run ruff check .

typecheck:
	uv run ty check

test:
	uv run pytest

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
