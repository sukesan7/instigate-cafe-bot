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

