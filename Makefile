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
	TTC_BOOTSTRAP_POST_LATEST="false"

# Scoped formatting/lint targets for Phase 2 (legacy icbot/ and main.py deferred to Phase 3/4)
SCOPED_STYLE_PATHS := tests scripts/manual tools/check_syntax.py tools/smoke_offline.py

# Incremental typed core modules verified clean under check_untyped_defs + warn_return_any
TYPED_CORE_MODULES := \
	icbot/config.py \
	icbot/permissions.py \
	icbot/embeds.py \
	icbot/app.py \
	icbot/utils.py \
	icbot/games \
	icbot/services/user_service.py \
	icbot/services/fun_market_catalog.py \
	icbot/services/gemini_client.py \
	icbot/services/sports_client.py \
	icbot/services/btc_esplora.py \
	icbot/services/crypto_fng.py \
	icbot/services/jobs_ats_client.py \
	icbot/services/ttc_client.py \
	icbot/services/yahoo_quotes.py \
	tools/check_syntax.py \
	tools/lore_loader.py \
	tools/scan_secrets.py \
	tools/smoke_offline.py

.PHONY: help format format-check lint lint-baseline typecheck typecheck-baseline smoke-offline test test-cov precommit audit check

help:
	@echo "Instigate Cafe Bot — Offline Developer Tooling"
	@echo ""
	@echo "Tier 1 (Static only — zero runtime, zero network):"
	@echo "  make format              Format tests/, scripts/manual/, and tools/smoke_offline.py only"
	@echo "  make format-check        Verify Ruff formatting on scoped paths"
	@echo "  make lint                Run Ruff lint on tests/, scripts/manual/, and tools/smoke_offline.py"
	@echo "  make lint-baseline       Report full Ruff baseline counts across icbot/, main.py, and tools/"
	@echo "  make typecheck           Run incremental mypy gate on verified clean core modules (21 files)"
	@echo "  make typecheck-baseline  Report full mypy baseline errors across icbot/, main.py, and tools/"
	@echo ""
	@echo "Tier 2 (Offline-safe — synthetic fixtures & mocked network only):"
	@echo "  make smoke-offline       AST-parse all Python files & verify zero-token offline module imports"
	@echo "  make test                Run offline pytest suite (excludes live_smoke)"
	@echo "  make test-cov            Run offline pytest suite with branch coverage on icbot/"
	@echo "  make precommit           Validate & run offline secret/hygiene/lint pre-commit hooks"
	@echo "  make check               Run full offline CI quality gate locally"
	@echo ""
	@echo "Tier 3 (Controlled network — PyPI Advisory DB only, never Discord/Railway):"
	@echo "  make audit               Run pip-audit against pinned requirements.lock"

format:
	$(RUFF) format $(SCOPED_STYLE_PATHS)

format-check:
	$(RUFF) format --check $(SCOPED_STYLE_PATHS)

lint:
	$(RUFF) check $(SCOPED_STYLE_PATHS)

lint-baseline:
	-$(RUFF) check icbot main.py tools --statistics

typecheck:
	$(MYPY) $(TYPED_CORE_MODULES)

typecheck-baseline:
	-$(MYPY) icbot main.py tools

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

audit:
	@echo "[Tier 3] Querying PyPI Advisory DB via pip-audit against requirements.lock..."
	$(PIP_AUDIT) -r requirements.lock

check: format-check lint typecheck smoke-offline precommit test
