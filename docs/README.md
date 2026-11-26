# Instigate Cafe Bot — Documentation Index

Documentation for **Instigate Cafe Bot** (`v1.0.0-rc1`), a self-hostable Discord slash-command bot built with Python 3.11–3.13, `discord.py` (`app_commands`), and SQLite 3 (WAL mode).

> [!IMPORTANT]
> **Self-Hosted Open-Source Application vs. Hosted Public Bot**
> - **Intended use (Self-Hosting):** Clone this repository, create your own Discord Application, configure only the optional integrations you want, and keep your SQLite database (`DB_PATH`) on your own host. No Discord `>100`-server verification is needed, and you act as the sole administrator/operator for your server.
> - **No Hosted Service Provided:** This project does not operate a public bot invite, managed hosting platform, or commercial support operation.
>
> **Campus & Regional Scope**
> - Core modules (`core`, `economy`, `casino`, `market`, `careers`, `ai`, `sports`, and `/ic exam`) work for any Discord community.
> - Default study links (`/ic notes`), grade distributions (`/ic grades` via `tmu.grades.zone`), default RateMyProfessors school (`RMP_UNIVERSITY`), and transit alert pollers (Metrolinx GO / TTC Mastodon) target **Toronto Metropolitan University (TMU)** and the Greater Toronto Area. See [External Providers & Campus Extensions](providers.md).

---

## Guides

### Users & Server Administrators
| Document | Audience | Contents |
| :--- | :--- | :--- |
| [Features & Command Guide](features.md) | Members & Admins | All 53 `/ic` slash commands, module profiles, exam calendars & `.ics`/CSV export, job search, resume consent flow, simulated ICSE market, and casino games. |
| [Screenshot Gallery](gallery.md) | All Readers | Real Discord UI screenshots covering onboarding, help menu, course notes, jobs, ICSE market, blackjack, and sports. |
| [Configuration Reference](configuration.md) | Operators & Admins | Environment variables in `icbot.config.Settings`, optional API keys vs. defaults, per-guild `/ic admin` overrides, feature profiles, and theme presets. |
| [Deployment, Troubleshooting & Recovery](deployment.md) | Self-Hosters & Operators | Discord Developer Portal OAuth2/intents setup, Docker Compose, `systemd`, Railway setup, SQLite WAL backups/restore, credential rotation, and troubleshooting. |

### Privacy, Architecture & Development
| Document | Audience | Contents |
| :--- | :--- | :--- |
| [Privacy & Data Model](privacy.md) | Users & Operators | Data-flow diagrams, SQLite table classification, RAM-only resume handling + Discord/Gemini boundaries, and `UserStore` export/erasure (`tools/privacy_ops.py`). |
| [System Architecture](architecture.md) | Contributors | `ServiceContainer` dependency injection, Mermaid sequence flows, 8-task `TaskSupervisor`, SQLite WAL v1–v7 migrations, and lifecycle/shutdown design. |
| [External Providers & Campus Extensions](providers.md) | Operators & Contributors | Generic vs. TMU/Toronto providers, rate limits, SSRF boundaries, attribution notes, and guide to adding a new campus adapter. |
| [Development, Testing & UI Conventions](development.md) | Contributors | `uv` dev setup, safety tiers, pre-commit & CI workflows, offline `pytest` harness, and Discord embed/interaction conventions. |
| [Security Policy](../SECURITY.md) | All Readers | Supported versions, vulnerability reporting, secret incident steps, and permission boundaries. |

---

## Project Links
- [Main README & Self-Hosting Quick Start](../README.md)
- [Contributing Guidelines](../CONTRIBUTING.md)
- [Community Code of Conduct](../CODE_OF_CONDUCT.md)
- [Changelog](../CHANGELOG.md)
- [MIT License](../LICENSE)
