# Instigate Cafe Bot — Development & Tooling Guide

This guide covers the canonical local development environment, operational safety tiers, quality gates, and offline CI workflow. For contribution etiquette and PR expectations, see [`CONTRIBUTING.md`](../CONTRIBUTING.md).

---

## 1. Python Support Matrix & Packaging (`uv`)

| Python Version | Metadata (`pyproject.toml`) | Offline CI Matrix | Notes |
| :--- | :--- | :--- | :--- |
| `< 3.10` | Unsupported | Not run | Excluded by `requires-python = ">=3.10,<3.14"`. |
| `3.10` | Minimum floor | Not run | Metadata floor only. |
| `3.11` | Supported | Tested | Matches Ruff `target-version = "py311"`. |
| `3.12` | **Primary Local** | Tested | Matches `[tool.mypy] python_version = "3.12"` and container runtime (`Dockerfile`). |
| `3.13` | Supported | Tested | Uses `audioop-lts>=0.2.2; python_version >= '3.13'` because stdlib `audioop` was removed in 3.13. |
| `3.14+` | Excluded (`<3.14`) | Not run | Native wheel compatibility (`numpy`, `matplotlib`, `aiohttp`). |

### Packaging Files
- [`pyproject.toml`](../pyproject.toml) — Canonical project metadata, runtime dependencies, `[dependency-groups] dev`, Pytest, Coverage, Ruff, and Mypy settings (`[tool.uv] package = false`).
- [`uv.lock`](../uv.lock) — Cross-platform locked dependency graph verified via `uv lock --check`.
- [`requirements.lock`](../requirements.lock) — Hash-pinned runtime export (`uv export --no-dev --no-emit-project -o requirements.lock`) for `pip-audit` and non-`uv` environments.

### Local Environment Provisioning
```bash
# Provision Python 3.12 virtualenv and install locked runtime + dev tools
uv sync --frozen --dev

# Install offline local pre-commit hooks
uv run pre-commit install

# Re-lock and re-export hashes after modifying dependencies in pyproject.toml
uv lock
uv export --no-dev --no-emit-project -o requirements.lock
```

---

## 2. Operational Safety Tiers (`Makefile`)

Developer commands in [`Makefile`](../Makefile) enforce explicit offline overrides (`DISCORD_TOKEN=""`, `ICBOT_ALLOW_GATEWAY="false"`, `LLM_ENABLED="false"`, `MARKET_WATCH_ENABLED="false"`, `BTC_WATCH_ENABLED="false"`, `JOBS_POLL_SECONDS="0"`):

| Safety Tier | Commands | Network / Side-Effect Boundary |
| :--- | :--- | :--- |
| **Tier 1: Static Only** | `uv run ruff check .`<br>`uv run ruff format --check .`<br>`uv run mypy .`<br>`uv run python tools/check_syntax.py`<br>`uv run python tools/check_docs.py` | Zero runtime execution, zero network calls, zero database mutations. |
| **Tier 2: Offline Runtime** | `uv run pytest`<br>`uv run python tools/smoke_offline.py`<br>`uv run python tools/smoke_staging.py`<br>`uv run python tools/scan_secrets.py --enforce-clean` | Synthetic temp SQLite databases, fake Discord interactions, mocked HTTP routers, and hard-blocked outbound IPv4/IPv6 sockets. |
| **Tier 3: Package Metadata Only** | `uv lock --check`<br>`uv run pip-audit --desc auto` | Contacts PyPI / OSV advisory metadata only. Never contacts Discord, Railway, or bot API providers. |
| **Tier 4: Live / Restricted** | `uv run python main.py --allow-gateway`<br>`tools/purge_global_commands.py`<br>`git push` to deploy branches | Never executed in CI, pre-commit, or automated tests. Requires explicit human operator action. |

---

## 3. Local Pre-Commit & GitHub Actions CI

### 3.1 Offline Pre-Commit Hooks ([`.pre-commit-config.yaml`](../.pre-commit-config.yaml))
All hooks use `repo: local` so they run 100% offline with pinned `.venv` binaries:
- `block-forbidden-files`: Blocks staging `.env*` (except `.env.example`), `*.db` / `-wal` / `-shm`, `*.sqlite*`, private keys (`*.pem`, `*.key`, `id_rsa*`, `id_ed25519*`), and private `lore.*.json` files.
- `scan-secrets-and-hygiene`: Executes `tools/scan_secrets.py --enforce-clean`.
- `block-private-key-blocks` & `check-merge-conflicts`: Rejects PEM headers and unresolved Git conflict markers.
- `check-syntax-ast-json-toml-yaml`: Executes `tools/check_syntax.py`.
- Ruff lint and format checks.

### 3.2 GitHub Actions Workflows
- **[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) (Offline Quality Gate):** Runs on push to `main`, PRs, and manual dispatch across Python `3.11`, `3.12`, and `3.13` with least-privilege `contents: read` permissions and zero live tokens. Runs `uv lock --check`, `scan_secrets.py --enforce-clean`, `pip-audit`, `pre-commit`, `ruff check`, `ruff format --check`, `mypy`, `smoke_offline.py`, usability audit tests, and full `pytest` branch coverage. Contains zero deploy steps.
- **[`.github/workflows/live-smoke.yml`](../.github/workflows/live-smoke.yml) (Optional Live Provider Smoke):** Never triggers on pull requests; gated behind explicit repository secrets (`OPT_IN_LIVE_SMOKE` / `GO_API_KEY`) and `ICBOT_ALLOW_GATEWAY=false`.
- **[`.github/dependabot.yml`](../.github/dependabot.yml):** Weekly dependency update checks for `pip` and `github-actions`.

---

## 4. Full Pre-PR Verification Sequence

Run this offline verification sequence before opening a pull request:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run python tools/check_docs.py
uv run python tools/scan_secrets.py --enforce-clean
uv run python tools/smoke_offline.py
uv run pytest
```

---

## 5. Secret Hygiene & Forbidden File Remediation

If `tools/scan_secrets.py --enforce-clean` flags an accidentally tracked database, private `.env` file, or private lore pack:

```bash
# Untrack without deleting your local file
git rm --cached data/dev.db data/dev.db-wal .env.local tools/lore.private.json

# Verify clean working tree and index
uv run python tools/scan_secrets.py --enforce-clean
```

If a real credential was committed, revoke and rotate it immediately at the provider console and follow [`SECURITY.md`](../SECURITY.md) and [Pre-Publication Security Remediation](deployment.md#91-pre-publication-security-and-history-remediation).
