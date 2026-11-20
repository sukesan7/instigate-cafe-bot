# Contributing to Instigate Cafe Bot

Thank you for your interest in contributing to the Instigate Cafe Bot! This project is a campus community Discord bot modernizing utility, transit, careers, and academic features for students.

---

## 1. Absolute Safety & Development Invariants

Before making any changes or submitting pull requests, you must adhere to these foundational safety rules:

> [!CAUTION]
> **Railway Autodeploy & Gateway Safeguards:**
> - **Never connect to the live Discord Gateway** during development or testing. Do not run `python main.py` or call `bot.run()` without explicit authorization.
> - **Never commit or push real secrets, `.env` files, or SQLite databases (`*.db`, `*.sqlite-wal`).**
> - **Never deploy to Railway or alter production deployment settings.**
> - Use fake Discord interactions (`FakeInteraction`, `FakeClient`), in-memory databases (`:memory:`), and mocked HTTP routers (`AiohttpMockRouter`) for all testing.

---

## 2. Getting Started with Local Development

We use [`uv`](https://docs.astral.sh/uv/) for fast, reproducible dependency management and virtual environments.

### 2.1 Prerequisites
- Python 3.11, 3.12, or 3.13
- `uv` installed (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

### 2.2 Setup Environment
```bash
# Clone your fork and enter the directory
git clone https://github.com/your-username/campus-bot.git
cd campus-bot

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

# 4. Run deterministic secret and publish-safety scan
uv run python tools/scan_secrets.py --enforce-clean

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
3. **Narrow Scope:** Keep PRs atomic and focused on a single change. Include tests verifying your change offline.
4. **No Direct Production Pushes:** All changes must land via reviewed pull requests passing the GitHub Actions offline quality gate.

---

## 5. Review & Maintainer Sign-Off Process

Every pull request requires:
- Approval from at least one repository maintainer.
- 100% green status on the GitHub Actions **Offline Quality Gate** (Ruff, Mypy, Pytest, Pip-Audit, Scan-Secrets).
- Clean diff verification ensuring no `.db`, `.env`, or credential artifacts are introduced.

