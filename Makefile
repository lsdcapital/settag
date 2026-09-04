.PHONY: help build update check check-site fix format lint typecheck test clean

help:
	@echo "Available targets:"
	@echo "  make build        - Set up development environment (Python and site)"
	@echo "  make update       - Bump Python packages, site packages, and pinned actions"
	@echo "  make check        - Run all quality checks (lint, format, typecheck, test, site)"
	@echo "  make check-site   - Typecheck and build the marketing site only"
	@echo "  make fix          - Auto-fix issues (ruff check --fix, ruff format)"
	@echo "  make format       - Format code with ruff"
	@echo "  make lint         - Lint code with ruff"
	@echo "  make typecheck    - Type check with ty"
	@echo "  make test         - Run pytest"
	@echo "  make clean        - Clean up cache files"
	@echo "  make help         - Show this help message"

build:
	uv sync --group dev
	cd site && pnpm install --frozen-lockfile

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
	@$(MAKE) --no-print-directory check-site
	@echo "\n========================================================"
	@echo "✅ All checks passed!"
	@echo "========================================================"

# The same steps site.yml runs: oxlint and oxfmt are the site's lint and
# format tools, tsc its type check. The grep guards against prerendering silently emitting an empty
# shell, which a successful build would not otherwise reveal.
check-site:
	@echo "\n========================================================"
	@echo "Running site lint, format check, typecheck, and build..."
	@echo "========================================================"
	cd site && pnpm lint
	cd site && pnpm format:check
	cd site && pnpm typecheck
	cd site && pnpm build
	@test -f site/dist/client/index.html
	@grep -aq "Nothing about your library leaves the machine." site/dist/client/index.html

format:
	uv run ruff format .
	cd site && pnpm format

fix:
	uv run ruff check . --fix
	uv run ruff format .
	cd site && pnpm lint --fix
	cd site && pnpm format

lint:
	uv run ruff check .
	cd site && pnpm lint

typecheck:
	uv run ty check

test:
	uv run pytest

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	rm -rf site/dist
