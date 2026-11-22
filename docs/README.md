# Instigate Cafe Bot — Documentation Index

Welcome to the documentation for **Instigate Cafe Bot** (`v1.0.0-rc1`), a modular, self-hostable Discord bot built with Python 3.11–3.13, `discord.py` (`app_commands`), and SQLite 3 (WAL mode).

> [!IMPORTANT]
> **Self-Hosted Open-Source Bot vs. Publicly Inviteable Hosted Bot**
> - **This repository is a self-hosted open-source release (recommended):** You clone the repo, register your own Discord Application, supply your own optional API keys, and retain full ownership of your local SQLite database (`DB_PATH`). No Discord >100-server verification is required.
> - **Hosted public invite deployments differ significantly:** Operating a single shared instance across >100 servers requires Discord Bot Verification, strict multi-tenant operational governance, disabled/restricted `@mention` Message Content Intent, and maintainer-funded API quotas. See [Deployment & Hosting Models](deployment.md#1-deployment-models-self-hosted-vs-hosted-public-invite).
>
> **Campus & Regional Scope**
> - Core modules (`core`, `economy`, `casino`, `market`, `careers`, `ai`, `sports`, and `/ic exam`) are **institution-agnostic**.
> - Default study guide links (`/ic notes`), grade distributions (`/ic grades` via `tmu.grades.zone`), default RateMyProfessors school (`RMP_UNIVERSITY`), and transit alert pollers (Metrolinx GO / TTC Mastodon) are **TMU / Greater Toronto Area specific** out of the box. See [External Providers & Campus Extensions](providers.md).

---

## Documentation Map

### Users & Server Administrators
| Document | Audience | Contents |
| :--- | :--- | :--- |
| [Features & Command Guide](features.md) | Students & Server Admins | Complete guide to all 53 `/ic` slash commands, module profiles, onboarding (`/ic getting-started`), exam scheduling & `.ics`/CSV export, job alerts, resume consent barrier, ICSE paper trading, and casino games. |
| [Configuration Reference](configuration.md) | Operators & Server Admins | Every environment variable in `icbot.config.Settings`, optional API keys vs. zero-key defaults, per-guild `/ic admin` overrides, feature profiles, and visual theme presets. |
| [Deployment, Hosting & Recovery](deployment.md) | Operators & Maintainers | Self-hosted vs. hosted public invite model, Discord OAuth2 scopes/intents, Docker Compose, systemd, Railway volume cutover/rollback, single-replica ownership, SQLite WAL online backups/restore, credential rotation, and pre-publication security checklist. |
| [Manual Staging Acceptance Runbook](staging-runbook.md) | Repository Owner / Operators | Owner-only manual staging setup (isolated bot token & synthetic DB), Railway safety audit, 11-family manual acceptance matrix, safe failure injection, immediate stop checklist, and human sign-off gate. |
| [Troubleshooting & Incident Runbook](troubleshooting.md) | Operators & Contributors | `/ic status` redacted diagnostics, startup & database path errors, Discord permissions/sync issues, 12-family failure taxonomy, provider circuit breakers, SQLite locks, and emergency AI killswitch. |

### Privacy, Security & Verification
| Document | Audience | Contents |
| :--- | :--- | :--- |
| [Privacy Model & Data Map](privacy.md) | Users, Admins & Auditors | Data-flow diagrams, table-by-table SQLite classification, zero-local-disk ephemeral resume processing, Discord/Gemini retention boundaries, operator disablement controls, and transactional export/erasure (`UserStore.delete_user_data`). |
| [Release Claims & Verification Evidence](release-evidence.md) | Maintainers & Reviewers | Traceability matrix mapping public, privacy, and operational claims to source files, automated offline tests, and explicit unverified live-staging boundaries. |
| [Release Verification Ledger & Blockers](release-verification.md) | Maintainers & Release QA | Measured offline verification ledger, unexecuted CI/Docker/live checks with reproducible commands, and pre-publication release-blockers checklist (including Git history review). |
| [Security Policy](../SECURITY.md) | All Readers | Supported versions, confidential vulnerability reporting, and incident response SLAs. |

### Developers & Contributors
| Document | Audience | Contents |
| :--- | :--- | :--- |
| [System Architecture & Developer Reference](architecture.md) | Maintainers & Contributors | Component & Mermaid sequence diagrams, zero-side-effect boot lifecycle, 8-task `TaskSupervisor`, SQLite WAL persistence & Schema v1–v7 migrations, `ServiceContainer` DI, and `JobsIngestionPipeline`. |
| [External Providers & Campus Extensions](providers.md) | Operators & Contributors | Catalog of TMU-specific vs. generic providers, optional API keys, rate limits, attribution/legal boundaries, SSRF URL safety, and 4-step guide for adding a new university adapter. |
| [Discord UI & Visual Style Contract](ui-style.md) | Contributors | Embed anatomy, theme presets, semantic status colors, Unicode/mention safety, and interaction-audience / component timeout matrix. |
| [Development & Tooling Guide](development.md) | Contributors | Canonical `uv` workflow, Python 3.11–3.13 support matrix, 4 operational safety tiers (`Makefile`, `.pre-commit-config.yaml`, offline CI), and secret-hygiene remediation commands. |
| [Offline Testing & Invariants Guide](testing.md) | Contributors & QA | Pytest marker taxonomy, `tests/conftest.py` network/env isolation, sanitized fixtures, domain invariants, and offline verification scripts (`smoke_offline.py`, `smoke_staging.py`, `check_docs.py`). |
| [Screenshots Capture & Redaction Guide](screenshots-guide.md) | Maintainers | Owner-run capture checklist and PII/ID cropping and redaction rules for populating the `README.md` gallery after staging verification. |

---

## Project Governance
- [Repository Overview & Quick Start](../README.md)
- [Contributing Guidelines](../CONTRIBUTING.md)
- [Code of Conduct](../CODE_OF_CONDUCT.md)
- [Changelog](../CHANGELOG.md)
- [License (MIT)](../LICENSE)
