.DEFAULT_GOAL := help

EMPTY :=
VENV_BIN := $(if $(wildcard .venv/bin/python),.venv/bin/,$(EMPTY))

PYTHON     ?= $(VENV_BIN)python
RUFF       ?= $(VENV_BIN)ruff
MYPY       ?= $(VENV_BIN)mypy
PYTEST     ?= $(VENV_BIN)pytest
PRE_COMMIT ?= $(VENV_BIN)pre-commit
PIP_AUDIT  ?= $(VENV_BIN)pip-audit

# Explicit offline safety environment for local checks (never reads live tokens/pollers)
SAFE_ENV := env \
	DISCORD_TOKEN="" \
	GEMINI_API_KEY="" \
	GO_API_KEY="" \
	ODDS_API_KEY="" \
	ALPHAVANTAGE_API_KEY="" \
	TTC_MASTODON_ACCT="" \
	LLM_ENABLED="false" \
	MARKET_WATCH_ENABLED="false" \
	MARKET_DAILY_REPORT_ENABLED="false" \
	BTC_WATCH_ENABLED="false" \
	JOBS_POLL_SECONDS="0" \
	WORKDAY_POLL_SECONDS="0" \
	JOBS_BOOTSTRAP_POST="false" \
	TTC_BOOTSTRAP_POST_LATEST="false" \
	ICBOT_ALLOW_GATEWAY="false"

# Repository formatting/lint and type-checking targets (respecting pyproject.toml excludes)
STYLE_PATHS := .
TYPECHECK_PATHS := .

.PHONY: help format format-check lint typecheck smoke-offline test test-cov precommit verify export-requirements audit check

help:
	@echo "Instigate Cafe Bot — Offline Developer Tooling"
	@echo ""
	@echo "Tier 1 (Static only — zero runtime, zero network):"
	@echo "  make format              Format repository Python files with Ruff"
	@echo "  make format-check        Verify Ruff formatting across repository"
	@echo "  make lint                Run Ruff lint across repository"
	@echo "  make typecheck           Run mypy across repository"
	@echo ""
	@echo "Tier 2 (Offline-safe — synthetic fixtures & mocked network only):"
	@echo "  make smoke-offline       AST-parse all Python files & verify zero-token offline module imports"
	@echo "  make test                Run offline pytest suite"
	@echo "  make test-cov            Run offline pytest suite with branch coverage on icbot/"
	@echo "  make precommit           Validate & run offline secret/hygiene/lint pre-commit hooks"
	@echo "  make verify              Run single-command offline contributor verification with diagnostics"
	@echo "  make export-requirements Regenerate requirements.lock and requirements.txt from uv.lock"
	@echo "  make check               Run lint, format, typecheck, offline smoke, precommit, and tests"
	@echo ""
	@echo "Tier 3 (Controlled network — PyPI Advisory DB only, never Discord/Railway):"
	@echo "  make audit               Run pip-audit against pinned requirements.lock"

format:
	$(RUFF) format $(STYLE_PATHS)

format-check:
	$(RUFF) format --check $(STYLE_PATHS)

lint:
	$(RUFF) check $(STYLE_PATHS)

typecheck:
	$(MYPY) $(TYPECHECK_PATHS)

smoke-offline:
	$(SAFE_ENV) $(PYTHON) tools/smoke_offline.py

test:
	$(SAFE_ENV) $(PYTEST)

test-cov:
	$(SAFE_ENV) $(PYTEST) --cov=icbot --cov-report=term-missing

precommit:
	$(PYTHON) tools/scan_secrets.py --enforce-clean
	$(PRE_COMMIT) validate-config .pre-commit-config.yaml
	$(PRE_COMMIT) run --all-files

verify:
	$(SAFE_ENV) $(PYTHON) tools/verify_offline.py

export-requirements:
	uv export --frozen --no-dev --no-emit-project -o requirements.lock
	uv export --frozen --no-dev --no-hashes --no-emit-project -o requirements.txt

audit:
	@echo "[Tier 3] Querying PyPI Advisory DB via pip-audit against requirements.lock..."
	$(PIP_AUDIT) -r requirements.lock

check: format-check lint typecheck smoke-offline precommit test
