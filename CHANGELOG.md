# Changelog

All notable changes to the Instigate Cafe Bot are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Planned
- Public alpha release and staging guild onboarding.
- Optional web dashboard for community event and exam scheduling.

---

## [0.2.4] - 2026-11-19

### Added
- **Maintainer, Privacy & Operational Documentation (Phase 12C):**
  - [`docs/architecture.md`](docs/architecture.md): High-level system architecture and Mermaid sequence diagrams for interaction routing, background task scheduling with circuit breakers, and atomic SQLite WAL economy transactions.
  - [`docs/configuration.md`](docs/configuration.md): Exhaustive environment reference covering all 33+ flags from `icbot.config.Settings` across 7 domains with defaults, types, and security/cost risks.
  - [`docs/providers.md`](docs/providers.md): External provider catalog (RMP, Metrolinx, TTC Mastodon, ATS engines, Gemini, Yahoo Finance, Esplora) and a 4-step contributor guide for adding campus providers safely without production credentials.
  - [`docs/privacy.md`](docs/privacy.md): Technical privacy specification detailing data minimization, classification, zero-disk ephemeral resume handling, third-party flows, and Right-to-be-Forgotten data deletion (`user_store.delete_user_data`).
  - [`docs/operations.md`](docs/operations.md): Operational guide covering health diagnostics (`/ic status`), log redaction, circuit breakers, schema v7 WAL maintenance, single-instance scheduler ownership, and hot database backup runbooks.
  - [`README.md`](README.md): Rebuilt open-source documentation with accurate feature matrix matching all 53 registered leaf commands, tested Python versions (3.11–3.13), and safe offline quick-start guides.
  - [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md): Original community code of conduct defining standards, moderation, and confidential reporting channels.
  - GitHub issue & PR templates: Added [`.github/ISSUE_TEMPLATE/feature_request.md`](.github/ISSUE_TEMPLATE/feature_request.md) and [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md) with explicit privacy warnings against pasting tokens or private server data.
- **Safe Production Hosting & Disaster Recovery (Phase 12B):**
  - Safe automated staging verification script [`tools/smoke_staging.py`](tools/smoke_staging.py) validating migrations, supervisor health, and container initialization offline.
  - Online hot backup and restore utilities [`tools/backup_db.py`](tools/backup_db.py) and [`tools/restore_db.py`](tools/restore_db.py) using SQLite VACUUM INTO and WAL checkpointing.
- **GitHub Actions Quality Gate (`.github/workflows/ci.yml`):** Pinned offline CI pipeline executing Ruff lint/format, Mypy static analysis, `pip-audit` dependency vulnerability scanning, secret checking, and Pytest coverage across Python 3.11, 3.12, and 3.13.
- **Optional Live Provider Smoke Workflow (`.github/workflows/live-smoke.yml`):** Credential-gated workflow for testing live third-party endpoints (RMP, Metrolinx, Mastodon) on manual trigger or low-frequency schedule, isolated from PR checks.
- **Dependabot Automation (`.github/dependabot.yml`):** Weekly automated dependency update tracking for Python packages and GitHub Actions.
- **Open-Source Governance & Safety Policies:** Created [`SECURITY.md`](SECURITY.md), [`CONTRIBUTING.md`](CONTRIBUTING.md), and [`CHANGELOG.md`](CHANGELOG.md).
- **Usability Audit Suite & Review Report:** Added [`tools/run_usability_audit.py`](tools/run_usability_audit.py), [`tests/unit/test_usability_review_archetypes.py`](tests/unit/test_usability_review_archetypes.py), and comprehensive evaluation documentation in [`docs/usability_review.md`](docs/usability_review.md) covering 4 user archetypes across 18 concrete tasks with 100% pass rate.
- **Diagnostics & Observability:** Implemented `RedactingLogFilter` scrubbing tokens, keys, and PII from logs, structured logging formatters with correlation IDs, provider outage dampeners, and `/ic status` sanitized issue report generation.

### Changed
- Enhanced `/ic botinfo` and `/ic getting-started` to explicitly document privacy policies, ephemeral memory-only resume handling, and data deletion instructions.
- Extended `tools/scan_secrets.py` with expanded forbidden patterns (`*.sqlite-wal`, `.env.*`, client secrets) and maintainer remediation instructions.

---

## [0.2.3] - 2026-11-19

### Added
- External provider reliability contracts (`ProviderProtocol`, `ProviderResult`, circuit breakers, exponential backoff with jitter).
- Deduplicated alert dispatcher (`icbot/tasks/alert_dispatcher.py`) preventing duplicate announcements across Discord text channels.
- Performance and memory profiling test harness (`tools/benchmark_profile.py`) verifying bounded memory consumption and execution limits.

---

## [0.2.2] - 2026-11-18

### Added
- Pure mathematical engines for casino games (Blackjack, Roulette, Slots) decoupled from Discord UI.
- Persistent Blackjack state machine with atomic restart crash recovery in SQLite.
- Guild-defined virtual stock exchange and market ledger with double-entry balance verification.

---

## [0.2.1] - 2026-11-17

### Added
- Confidential, privacy-first AI resume review (`/ic resume`) processing PDF documents ephemerally in memory with zero disk persistence.
- Privacy-respecting mention assistant with synthetic lore compartmentalization and zero channel message eavesdropping.
- Gemini API governance wrapper with token budget enforcement, rate limiting, and graceful offline fallback.

---

## [0.2.0] - 2026-11-15

### Added
- Re-architected modular service container (`ServiceContainer`) with strict dependency injection.
- Reversible SQLite schema migration system up to schema v4 with WAL journal mode and ACID ledger.
- Academic tools suite: course notes hub, RateMyProfessors ratings, exam scheduling with iCalendar export.
- Job alerts pipeline with explainable rules engine and ATS adapters (Greenhouse, Workday).
- Local testing framework (`tests/helpers.py`) with fake Discord interactions and offline network mocking.

---

## [0.1.0] - 2026-10-21

### Added
- Initial historical baseline monolithic implementation of Instigate Cafe Bot.

