# Contributing to Instigate Cafe Bot

Thank you for your interest in contributing to the Instigate Cafe Bot! This project is a campus community Discord bot modernizing utility, transit, careers, and academic features for students.

---

## 1. Contributor Safety & Testing Standards

Before making changes or opening a pull request, follow these project invariants:

> [!CAUTION]
> **Offline Testing & Data Safety:**
> - **Never connect to the live Discord Gateway** during ordinary development or testing (`ICBOT_ALLOW_GATEWAY=false` by default). Do not run `python main.py --allow-gateway` or call `bot.run()` in tests or imports.
> - **Never commit secrets or private data:** Do not commit `.env` files, API tokens, SQLite databases (`*.db`, `*-wal`, `*-shm`), private server lore, real user messages, or student resumes into source, fixtures, logs, or issue reports.
> - **Mock external APIs by default:** Use fake Discord interactions (`FakeInteraction`, `FakeClient`), temporary SQLite databases (`tmp_path`), and mocked HTTP routers (`AiohttpMockRouter`). Never infer that tests pass from syntax checks alone.
> - **Deployments are strictly human-controlled:** Never trigger production deployments or alter hosting settings from CI or PRs.

---

## 2. Getting Started with Local Development

We use [`uv`](https://docs.astral.sh/uv/) for fast, reproducible dependency management and virtual environments. For full tooling details and safety tiers, see the [Development & Tooling Guide](docs/development.md) and [Offline Testing Guide](docs/testing.md).

### 2.1 Prerequisites
- Python 3.11, 3.12, or 3.13
- `uv` installed (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

### 2.2 Setup Environment
```bash
# Clone your fork and enter the directory
git clone https://github.com/sukesan7/instigate-cafe-bot.git
cd instigate-cafe-bot

# Install locked dependencies and development tooling
uv sync --dev

# Install git pre-commit hooks
uv run pre-commit install
```

---

## 3. Local Quality Gates & Verification

Before submitting a pull request, verify that all local quality checks pass offline:

```bash
# 1. Run formatting and linting
uv run ruff check .
uv run ruff format --check .

# 2. Run static type checking
uv run mypy .

# 3. Run zero-token offline smoke check
uv run python tools/smoke_offline.py

# 4. Run deterministic secret, docs, and command-parity scans
uv run python tools/scan_secrets.py --enforce-clean
uv run python tools/check_docs.py
uv run python tools/check_command_docs.py

# 5. Run the offline pytest suite
uv run pytest

# 6. Run the 4-archetype usability audit benchmark
uv run python tools/run_usability_audit.py
```

---

## 4. Pull Request & Commit Guidelines

1. **Branch Naming:** Create focused feature branches (e.g. `feature/academic-notes-filter`, `fix/resume-timeout`).
2. **Conventional Commits:** Write clear, structured commit messages:
   - `feat(...)`: New user-facing features or command sub-commands.
   - `fix(...)`: Evidenced bug fixes and error handling improvements.
   - `docs(...)`: Documentation, checklists, and guides.
   - `test(...)`: Unit, integration, or benchmark tests.
   - `chore(...)`: Tooling, dependency, or configuration updates.
3. **Narrow Scope & Verification:** Keep PRs focused on a single change with offline regression tests.
   - **Schema changes:** Use synthetic temp fixtures, write backward-compatible migrations, and verify backups/restore cleanly.
   - **Economy & market changes:** Assert transactional atomicity (`BEGIN IMMEDIATE`) and zero duplicate settlement or balance drift.
   - **Module cleanup:** Verify dynamic slash-command registration (`icbot/registry.py`) and CLI/test entrypoints before removing any module.
4. **No Direct Production Pushes:** All changes land via reviewed pull requests passing the GitHub Actions offline quality gate. Production deployments are triggered manually by maintainers.

---

## 5. Review & Maintainer Sign-Off Process

Every pull request requires:
- Approval from at least one repository maintainer.
- 100% green status on the GitHub Actions **Offline Quality Gate** (Ruff, Mypy, Pytest, Pip-Audit, Scan-Secrets).
- Clean diff verification ensuring no `.db`, `.env`, or credential artifacts are introduced.

---

## 6. Adding a New Campus Provider

We welcome community contributions expanding the bot's academic tools to other universities! Please consult the [Provider Integration Guide](docs/providers.md#4-contributor-guide-adding-a-new-campus-provider) for step-by-step instructions on implementing `AcademicProviderProtocol`, SSRF validation, and writing offline tests with synthetic fixtures.

---

## 7. Code of Conduct

All contributors and community participants are expected to adhere to our [Community Code of Conduct](CODE_OF_CONDUCT.md). Please report any concerns to **`imsukesan@gmail.com`**.

