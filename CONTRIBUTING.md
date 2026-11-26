# Contributing to Instigate Cafe Bot

Thank you for contributing to Instigate Cafe Bot! This is a community-built, self-hostable Discord bot, and keeping contributions practical, well-tested, and safe to run offline helps every maintainer and self-hoster.

---

## 1. Contributor Safety Rules

> [!CAUTION]
> **Offline Testing & Data Safety:**
> - **Never connect to the live Discord Gateway** during development or automated tests (`ICBOT_ALLOW_GATEWAY=false` by default). Do not run `python main.py --allow-gateway` or call `bot.run()` inside tests or imports.
> - **Never commit secrets or private community data:** Keep `.env` files, API tokens, SQLite databases (`*.db`, `*-wal`, `*-shm`), private server lore, real Discord messages, and real student resumes out of code, fixtures, logs, and issue reports.
> - **Mock all external APIs in tests:** Use fake Discord interactions (`FakeInteraction`, `FakeClient`), temporary SQLite databases (`tmp_path`), and `AiohttpMockRouter`.
> - **Keep database changes reversible:** Test SQLite schema changes against synthetic temp databases and confirm `tools/backup_db.py` backups remain valid.

---

## 2. Setting Up a Contributor Environment

See the [Development & Testing Guide](docs/development.md) for Python matrix notes, safety tiers, UI conventions, and testing architecture.

### Prerequisites
- **Python 3.11–3.13** (`3.12` recommended; on rolling Linux distros whose system Python is 3.14+, let `uv` manage Python 3.12).
- [`uv`](https://docs.astral.sh/uv/) package manager.

### Setup (`uv`)

```bash
git clone https://github.com/sukesan7/instigate-cafe-bot.git
cd instigate-cafe-bot

uv venv --python 3.12
uv sync --frozen --dev

# Install offline local pre-commit hooks
uv run pre-commit install
```

### Standard Python + `pip` Developer Fallback

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install pytest pytest-asyncio pytest-cov aioresponses ruff mypy pre-commit pip-audit
```

*(Never hand-edit `requirements.txt` or `requirements.lock`; update `pyproject.toml`, run `uv lock`, and regenerate them with `make export-requirements`.)*

---

## 3. Running Tests, Linting & Checks

Run the single-command offline verification suite before opening a pull request:

```bash
uv run python tools/verify_offline.py
# Or: make verify
```

Individual checks:

```bash
uv run pytest                                  # Full offline test suite (mocked network)
uv run ruff check . && uv run ruff format --check .
uv run mypy .
uv run python tools/smoke_offline.py           # AST check & zero-token import verification
uv run python tools/scan_secrets.py --enforce-clean
uv run python tools/check_docs.py              # Markdown links, images, and anchors
uv run python tools/check_command_docs.py      # /ic command-tree parity vs docs/features.md
uv run pip-audit -r requirements.lock          # Networked PyPI/OSV advisory check (or: make audit)
```

---

## 4. Pull Request Expectations

1. **Focused branches:** Keep PRs scoped to one fix or feature, with offline tests covering any changed behavior.
2. **Command & setting changes:** If you add/edit a `/ic` command or `Settings` option, update [Features & Command Guide](docs/features.md) or [Configuration Reference](docs/configuration.md) and confirm `check_command_docs.py` and `check_docs.py` pass.
3. **Adding a campus provider:** Follow the step-by-step protocol and SSRF safety checklist in [External Providers Guide](docs/providers.md#4-contributor-guide-adding-a-new-campus-provider).
4. **PR checklist & review:** Ensure CI (`Quality & Security Gate` across Python `3.11–3.13` and `dependency-audit`) passes cleanly and no `.db`, `.env`, or credential artifacts appear in your diff.

---

## 5. Code of Conduct

Participants are expected to follow our [Community Code of Conduct](CODE_OF_CONDUCT.md). Report concerns confidentially to **`imsukesan@gmail.com`**.
