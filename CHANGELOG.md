# Changelog

All notable changes to the Instigate Cafe Bot are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Changed
- Consolidated repository documentation into a flat 12-file public `docs/` layout anchored by [`docs/README.md`](docs/README.md) (`features.md`, `configuration.md`, `deployment.md`, `troubleshooting.md`, `architecture.md`, `providers.md`, `privacy.md`, `ui-style.md`, `development.md`, `testing.md`, `release-evidence.md`) and moved historical planning, audit, and launch reports to an out-of-repo owner archive.

### Planned
- Public deployment and automated production migration.
- Optional web dashboard for community event and exam scheduling.

---

## [1.0.0-rc1] - 2026-11-20

### Added
- **Private Alpha & Public Beta Readiness (Phase 13B):**
  - **Release Candidate Version:** Bumped package and bot version to `v1.0.0-rc1` (`1.0.0rc1` in `pyproject.toml`).
  - **Automated Release Gate Verifier ([`tools/verify_beta_readiness.py`](tools/verify_beta_readiness.py)):** Offline evaluation tool testing all 8 mandatory release gates:
    - `GATE-1`: Multi-Discipline Onboarding & Autonomous Setup (CS/Engineering vs. Business/Commerce cohorts).
    - `GATE-2`: Multi-Guild Data, Settings & Economy Isolation (Guild Alpha vs. Guild Beta).
    - `GATE-3`: Security, Clean Secret Scan & Log Redaction (`tools/scan_secrets.py --enforce-clean` + `RedactingLogFilter`).
    - `GATE-4`: Financial Economy Integrity & ACID Settlement (Zero balance drift, 10-way double-claim concurrency lock, ledger audit trail).
    - `GATE-5`: Bounded Interactive Response Latency (< 2500ms response/deferral token deadline).
    - `GATE-6`: Predictable Provider Failure & Graceful Degradation (User-friendly fallback guidance, zero unhandled 5xx tracebacks).
    - `GATE-7`: Cost Controls, Quota Budgets & Feature-Flag Toggles (`student_core` profile).
    - `GATE-8`: Privacy-Preserving Feedback & RTBF Data Deletion Compliance (`ResumeConfirmView` consent disclosure, `delete_user_data` profile purge with audit ledger preservation).
  - **Alpha/Beta Readiness Specification** *(historical planning doc `docs/alpha-beta-readiness.md`, archived out of repo; verification boundaries maintained in [`docs/release-evidence.md`](docs/release-evidence.md))*: Multi-discipline cohort specifications, 8 measurable release gate matrices, privacy feedback ingestion rules, bug triage SLA definitions (P0–P3), and feature-flag degradation runbooks.
  - **Integration Test Suite ([`tests/integration/test_beta_readiness_and_release_gates.py`](tests/integration/test_beta_readiness_and_release_gates.py)):** Pytest coverage asserting 100% pass rate across all 8 release gates.

---

## [0.2.5] - 2026-11-19

### Added
- **Staging Acceptance & Fault-Injection Protocol (Phase 13A):**
  - [`tools/run_staging_acceptance.py`](tools/run_staging_acceptance.py): Automated offline acceptance and fault-injection harness executing a complete 27-scenario matrix covering multi-guild isolation, missing API credentials, exam CRUD, jobs mock subscriptions, atomic money settlement, alert deduplication, restart scheduler uniqueness, server permissions, and command timeouts.
  - Controlled failure injections: Network partitions (`aiohttp.ClientConnectorError`), invalid credentials (`HTTP 401/403` with secret scrubbing), rate limits (`HTTP 429` with `Retry-After`), Discord API errors (`403/404`), SQLite database lock contention, disk full (`OSError: [Errno 28]`), process crash recovery mid-transaction, DST boundary shifts, and double-click race conditions.
  - Disaster recovery: Point-in-time online hot backups, restore verification, and schema v1–v7 forward migrations.
  - Historical runbook and severity matrix *(formerly `docs/staging-acceptance-and-fault-injection.md`, archived out of repo; test harness usage documented in [`docs/testing.md`](docs/testing.md))*.
  - Automated integration test suite in [`tests/integration/test_staging_acceptance_and_fault_injection.py`](tests/integration/test_staging_acceptance_and_fault_injection.py) asserting 100% pass rate with zero open P0/P1 defects.

---

## [0.2.4] - 2026-11-19

### Added
- **Maintainer, Privacy & Operational Documentation (Phase 12C):**
  - [`docs/architecture.md`](docs/architecture.md): High-level system architecture and Mermaid sequence diagrams for interaction routing, background task scheduling with circuit breakers, and atomic SQLite WAL economy transactions.
  - [`docs/configuration.md`](docs/configuration.md): Exhaustive environment reference covering all 33+ flags from `icbot.config.Settings` across 7 domains with defaults, types, and security/cost risks.
  - [`docs/providers.md`](docs/providers.md): External provider catalog (RMP, Metrolinx, TTC Mastodon, ATS engines, Gemini, Yahoo Finance, Esplora) and a 4-step contributor guide for adding campus providers safely without production credentials.
  - [`docs/privacy.md`](docs/privacy.md): Technical privacy specification detailing data minimization, classification, zero-disk ephemeral resume handling, third-party flows, and Right-to-be-Forgotten data deletion (`user_store.delete_user_data`).
  - [`docs/deployment.md`](docs/deployment.md) & [`docs/troubleshooting.md`](docs/troubleshooting.md) *(consolidated from earlier `docs/operations.md`)*: Operational guides covering health diagnostics (`/ic status`), log redaction, circuit breakers, Schema v7 WAL maintenance, single-instance scheduler ownership, and hot database backup runbooks.
  - [`README.md`](README.md): Rebuilt open-source documentation with accurate feature matrix matching all 53 registered leaf commands, tested Python versions (3.11–3.13), and safe offline quick-start guides.
  - [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md): Original community code of conduct defining standards, moderation, and confidential reporting channels.
  - GitHub issue & PR templates: Added [`.github/ISSUE_TEMPLATE/feature_request.md`](.github/ISSUE_TEMPLATE/feature_request.md) and [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md) with explicit privacy warnings against pasting tokens or private server data.
- **Safe Production Hosting & Disaster Recovery (Phase 12B):**
  - Safe automated staging verification script [`tools/smoke_staging.py`](tools/smoke_staging.py) validating migrations, supervisor health, and container initialization offline.
  - Online hot backup and restore CLI [`tools/backup_db.py`](tools/backup_db.py) (`backup`, `restore`, `verify` subcommands) using SQLite `VACUUM INTO` and WAL checkpointing.
- **GitHub Actions Quality Gate (`.github/workflows/ci.yml`):** Pinned offline CI pipeline executing Ruff lint/format, Mypy static analysis, `pip-audit` dependency vulnerability scanning, secret checking, and Pytest coverage across Python 3.11, 3.12, and 3.13.
- **Optional Live Provider Smoke Workflow (`.github/workflows/live-smoke.yml`):** Credential-gated workflow for testing live third-party endpoints (RMP, Metrolinx, Mastodon) on manual trigger or low-frequency schedule, isolated from PR checks.
- **Dependabot Automation (`.github/dependabot.yml`):** Weekly automated dependency update tracking for Python packages and GitHub Actions.
- **Open-Source Governance & Safety Policies:** Created [`SECURITY.md`](SECURITY.md), [`CONTRIBUTING.md`](CONTRIBUTING.md), and [`CHANGELOG.md`](CHANGELOG.md).
- **Usability Audit Suite & Review Report:** Added [`tools/run_usability_audit.py`](tools/run_usability_audit.py) and [`tests/unit/test_usability_review_archetypes.py`](tests/unit/test_usability_review_archetypes.py) covering 4 user archetypes across 18 concrete tasks with 100% pass rate *(historical evaluation report `docs/usability_review.md` archived out of repo; benchmark command documented in [`docs/testing.md`](docs/testing.md))*.
- **Diagnostics & Observability:** Implemented `RedactingLogFilter` scrubbing tokens, keys, and PII from logs, structured logging formatters with correlation IDs, provider outage dampeners, and `/ic status` sanitized issue report generation.

### Changed
- Enhanced `/ic botinfo` and `/ic getting-started` to explicitly document privacy policies, ephemeral memory-only resume handling, and data deletion instructions.
- Extended `tools/scan_secrets.py` with expanded forbidden patterns (`*.sqlite-wal`, `.env.*`, client secrets) and maintainer remediation instructions.

---

## [0.2.3] - 2026-11-19

### Added
- External provider reliability contracts (`ProviderProtocol`, `ProviderResult`, circuit breakers, exponential backoff with jitter).
- Deduplicated alert helper (`icbot/services/alert_dedupe.py`) preventing duplicate announcements across Discord text channels.
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

