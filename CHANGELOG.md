# Changelog

All notable changes to Instigate Cafe Bot are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Changed
- Removed obsolete release/benchmark/live-smoke scaffolding (`tools/run_staging_acceptance.py`, `tools/run_usability_audit.py`, `tools/verify_beta_readiness.py`, `tools/benchmark_profile.py`, `tools/benchmark_db.py`, `tools/run_live_smoke.py`, `.github/workflows/live-smoke.yml`, `tests/live_smoke/`) and unused `aioresponses` dev dependency after extracting uncovered fault-injection, concurrency, recovery, and permission regressions into integration tests; streamlined `.pre-commit-config.yaml`, `Makefile`, and `.github/workflows/ci.yml` to eliminate duplicate test/typecheck runs.
- Consolidated repository documentation into [`docs/`](docs/README.md) (`architecture.md`, `configuration.md`, `deployment.md`, `development.md`, `features.md`, `gallery.md`, `privacy.md`, `providers.md`), merged testing and UI conventions into [`docs/development.md`](docs/development.md), and merged troubleshooting into [`docs/deployment.md`](docs/deployment.md).
- Rebuilt [`README.md`](README.md) to separate ordinary self-hosting setup (`uv sync --frozen --no-dev` / `pip`, `.env.example`, `--allow-gateway` opt-in, Discord Developer Portal setup) from contributor development tooling in [`CONTRIBUTING.md`](CONTRIBUTING.md).
- Hardened SSRF boundaries across `icbot/academics/url_safety.py`, `BoundedHttpTransport`, academic providers, and `WorkdayAdapter` with connector-level DNS IP validation, bounded redirect validation, and streaming response byte caps.
- Standardized Python packaging around `pyproject.toml` + `uv.lock` with generated `requirements.lock` and `requirements.txt` (`make export-requirements`, supported Python `3.11–3.13`), and added `tools/verify_offline.py` (`make verify`) for offline contributor checks.
- Renamed manual live network probes from `scripts/manual/test_*.py` to `scripts/manual/probe_*.py` to prevent accidental pytest collection, moved and hardened `tools/purge_global_commands.py` into `scripts/admin/purge_global_commands.py` (default `--dry-run`, `--confirm` protection, secret redaction).

---

## [1.0.0-rc1] - 2026-11-20

### Added
- Bumped package and bot version to `v1.0.0-rc1` (`1.0.0rc1` in `pyproject.toml`).
- Multi-guild SQLite isolation, schema v7 migrations, interactive `/ic admin setup`, per-guild feature profiles (`default`, `student_core`, `toronto_tmu`, `community_full`), visual theme presets, and transactional user export/erasure (`UserStore.export_user_data`, `UserStore.delete_user_data`, `tools/privacy_ops.py`).

---

## [0.2.5] - 2026-11-19

### Added
- Offline synthetic fault-injection and lifecycle verification harnesses (`tools/smoke_staging.py`, `tools/run_staging_acceptance.py`, `tools/verify_beta_readiness.py`).
- Disaster recovery CLI tooling (`tools/backup_db.py`) supporting online SQLite WAL hot backups, integrity verification, and restores.

---

## [0.2.4] - 2026-11-19

### Added
- Technical documentation across architecture, configuration, external providers, privacy data flows, and deployment operations.
- GitHub Actions `Quality & Security Gate` workflow (`.github/workflows/ci.yml`), manual credential-gated `live-smoke.yml`, `.github/dependabot.yml`, `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, and issue/PR templates.
- Log/error secret redaction (`RedactingLogFilter`), provider outage dampening (`ProviderOutageTracker`), and sanitized `/ic status issue_report:True` diagnostics.

---

## [0.2.3] - 2026-11-19

### Added
- External provider contracts (`ProviderProtocol`, `ProviderResult`), circuit breakers, exponential backoff with jitter, and deduplicated alert helpers (`icbot/services/alert_dedupe.py`).
- Performance and memory profiling script (`tools/benchmark_profile.py`).

---

## [0.2.2] - 2026-11-18

### Added
- Pure Python PRNG casino engines (`blackjack_engine.py`, `roulette_engine.py`, `slots_engine.py`) decoupled from Discord views, with persistent SQLite Blackjack crash recovery.
- Guild-scoped simulated stock exchange (`icse`) and double-entry SQLite ledger.

---

## [0.2.1] - 2026-11-17

### Added
- Ephemeral AI resume review (`/ic resume`) processing attachments in memory with a mandatory consent barrier (`ResumeConfirmView`) and an offline No-AI checklist.
- Optional `@mention` assistant with synthetic example lore (`tools/lore.example.json`), rate limits, and token caps.

---

## [0.2.0] - 2026-11-15

### Added
- Modular `ServiceContainer` dependency injection architecture and `TaskSupervisor`.
- SQLite WAL schema migration system (`icbot/db.py`).
- Academic toolkit (`/ic notes`, `/ic prof`, `/ic grades`, `/ic exam`) and ATS job ingestion (`Greenhouse`, `Workday`, `Lever`).
- Offline test harness (`tests/helpers.py`) with mocked Discord interactions and socket-blocked HTTP mocking.

---

## [0.1.0] - 2026-10-21

### Added
- Initial baseline single-script Discord bot implementation.
