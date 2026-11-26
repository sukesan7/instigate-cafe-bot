# Instigate Cafe Bot — Release Verification Ledger & Release-Blockers Checklist

**Target Release:** `v1.0.0-rc1`  
**Verification Scope:** Reproducible non-production offline quality gate (zero live Discord gateway, zero production `.env` secrets, zero Railway deploys).  
**Companion Traceability Matrix:** [Release Claims & Verification Evidence](release-evidence.md)

---

## 1. Measured Local Verification Environment

| Component | Version / Status |
| :--- | :--- |
| **Managed Python (tested locally)** | `Python 3.12.13` (`.venv/bin/python`) |
| **Host System Python (excluded `<3.14`)** | `Python 3.14.10` (Arch/Garuda host `/usr/bin/python3` — intentionally not used) |
| **Package Manager** | `uv 0.13.9` (`uv.lock` frozen, 93 locked packages) |
| **Pytest / Plugins** | `pytest 9.2.0`, `pytest-asyncio 1.4.1.post1`, `pytest-cov 7.1.0` |
| **Docker CLI / Daemon** | `Docker 29.10.0` CLI present; **daemon unavailable in offline sandbox** (`/var/run/docker.sock` absent) |

---

## 2. Offline Verification Evidence Ledger (Measured Outputs)

All commands below were executed offline with `ICBOT_ALLOW_GATEWAY=false`, blank secret environment variables, synthetic temporary SQLite databases, and hard-blocked outbound IPv4/IPv6 sockets (`tests/conftest.py` / `tools/smoke_offline.py`):

| Check | Exact Command | Result | Pass / Fail / Xfail / Skip | Elapsed |
| :--- | :--- | :---: | :--- | :---: |
| **Lockfile consistency** | `uv lock --check` | PASS | 0 drift (`uv.lock` & generated `requirements.txt` / `requirements.lock`) | `0.02s` |
| **Secret & publish-safety scan** | `uv run python tools/scan_secrets.py --enforce-clean` | PASS | 0 violations (working tree clean) | `0.23s` |
| **Offline AST & import smoke** | `uv run python tools/smoke_offline.py` | PASS | 173+ AST-checked `.py` files, 17 modules, 53 `/ic` leaves, 0 network side effects | `1.6s` |
| **Offline staging & WAL migration smoke** | `uv run python tools/smoke_staging.py` | PASS | Schema v7 WAL migration, `ServiceContainer`, 8 tasks, clean shutdown | `0.2s` |
| **Docs link, image & anchor check** | `uv run python tools/check_docs.py` | PASS | 0 broken links, images, anchors, workstation file URIs, or placeholders | `0.1s` |
| **Command-tree docs parity** | `uv run python tools/check_command_docs.py` | PASS | 53 live `/ic` commands = 53 documented rows, exact option & theme parity | `1.3s` |
| **Ruff lint & formatting** | `uv run ruff check . && uv run ruff format --check .` | PASS | 0 lint findings, all files formatted | `0.1s` |
| **Mypy static typing** | `uv run mypy .` | PASS | 0 errors across tracked source files | `0.5s` |
| **Offline pytest collection** | `uv run pytest --collect-only -q` | PASS | Deterministic offline collection (0 network side effects; `live_smoke` excluded) | `1.9s` |
| **Full offline test suite + branch coverage** | `uv run pytest --cov=icbot --cov-report=term -q` | PASS | **0 failed, 0 xfailed, 0 skipped** (see latest run summary below; measured **80%** combined statement+branch coverage across `icbot/`) | `~26s` |
| **Unified offline contributor gate** | `uv run python tools/verify_offline.py` | PASS | 11/11 stages green | `~22s` |

### Core Domain Gates Verified Inside Offline Pytest
- **Legacy-to-v7 migrations, WAL integrity & backups:** `tests/unit/test_deployment_and_hosting_safety.py`, `tests/integration/test_resilient_state_and_lifecycle.py`
- **User data export & RTBF deletion/anonymization privacy:** `tests/unit/test_privacy_model.py`, `tests/unit/test_llm_assistant_privacy_and_lore.py`
- **Per-guild permissions, DM rejection & private exam ephemeral isolation:** `tests/integration/test_guild_setup_and_permissions.py`, `tests/integration/test_exam_ux_and_boundaries.py`, `tests/unit/test_release_claims_and_permissions.py`
- **Embed payload bounds, theme contrast heuristic, HTTPS-only jobs & mention safety:** `tests/unit/test_branding_and_themes.py`, `tests/integration/test_jobs_ui_and_adversarial_content.py`, `tests/integration/test_interaction_state_and_pagination.py`
- **12-family failure taxonomy, redaction, circuit breakers & provider degradation:** `tests/unit/test_failure_taxonomy_and_reliability.py`, `tests/integration/test_pollers_and_http_errors.py`
- **Docs, command parity, packaging, tooling & refactor invariants:** `tests/unit/test_docs_validation.py`, `tests/unit/test_command_docs_parity.py`, `tests/unit/test_packaging_and_reproducibility.py`, `tests/unit/test_tooling_and_admin_scripts.py`, `tests/unit/test_refactoring_characterization.py`, `tests/unit/test_release_quality_gate.py`

---

## 3. Verification Tiers & Checks Not Executed Locally

Distinguish explicitly among three separate verification tiers—local offline passes must never be cited as container or live Discord acceptance:

| Tier | Scope | Local Sandbox Status | Reproducible Reference |
| :--- | :--- | :---: | :--- |
| **Offline Staging Simulation** | Synthetic `tmp_path` volume tests, `tools/smoke_staging.py`, WAL Schema v1–v7 upgrades, `DatabasePathError`/`MigrationError` fail-closed guards, and `tools/backup_db.py` | **Executed Locally (PASS)** | `uv run python tools/smoke_staging.py` & `uv run python tools/verify_offline.py` |
| **Container Smoke** | Real Docker image build (`uv:0.13.9` + `uv.lock`), two-boot `/data/campus.db` persistence & non-root `UID 10001` file ownership with `ICBOT_ALLOW_GATEWAY=false` | **Not Executed Locally** *(Docker daemon `/var/run/docker.sock` absent)* | Multi-boot throwaway-volume plan in [`docs/deployment.md`](deployment.md#42-setup-and-offline-container-verification-commands) |
| **Authenticated Live Discord Acceptance** | Real staging Discord bot token, live guild slash-command sync, and end-to-end provider round-trips | **Not Executed Locally** *(prohibited in automated runs)* | Manual owner checklist in [`docs/staging-runbook.md`](staging-runbook.md) |

The following environment-dependent checks are **not executed** inside this offline sandbox (`systemd-analyze verify` also failed to create `/run/systemd/` on the sandbox's read-only `/run` filesystem, though strict Python INI parsing and `Settings.load_validated()` env validation pass locally):

| Check | Status | Why Not Executed Locally | Exact Reproducible Command / Workflow |
| :--- | :--- | :--- | :--- |
| **Python 3.11 & 3.13 full test matrix** | **Not Executed Locally** | Only `Python 3.12.13` venv deps are cached locally without PyPI network access. | Trigger [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) (`matrix.python-version: ["3.11", "3.12", "3.13"]`) or run locally on a host with PyPI access: `uv venv --python 3.13 .venv313 && UV_PROJECT_ENVIRONMENT=.venv313 uv sync --frozen --dev && .venv313/bin/python tools/verify_offline.py` |
| **Dependency vulnerability audit (`pip-audit`)** | **Not Executed Locally** | Queries PyPI OSV advisory API (no network in offline sandbox). | Executed in `.github/workflows/ci.yml` (`dependency-audit` job) or locally with network: `uv run pip-audit --desc auto` (or `make audit`) |
| **Docker image build & 2-boot `/data` persistence smoke** | **Not Executed Locally** | Docker daemon socket `/var/run/docker.sock` is unavailable in sandbox. | Run on host with Docker daemon using throwaway `/data` volume plan in [`docs/deployment.md`](deployment.md#42-setup-and-offline-container-verification-commands) |
| **`systemd-analyze verify` daemon check** | **Not Executed Locally** | Sandbox `/run/systemd/` filesystem is read-only (`Failed to create directory '/run/systemd/'`); INI structure and `Environment=` settings pass offline unit tests. | Run on target Linux host with systemd: `systemd-analyze verify ./systemd/campus-bot.service` |
| **Live Discord Gateway sync & upstream API round-trips** | **Not Executed Locally** | Strictly prohibited in automated runs; requires owner-supplied staging credentials. | Manual owner staging procedure in [`docs/staging-runbook.md`](staging-runbook.md) |

---

## 4. Release-Blockers Checklist (Owner Sign-Off Required)

Check each item against actual CI run URLs and manual verification evidence before publishing the repository or deploying to production:

- [ ] **BLOCKER 1 — Explicit Git History Sensitivity Review (CRITICAL before public visibility):**  
  A clean working tree (`tools/scan_secrets.py --enforce-clean`) or source ZIP does **not** prove Git history is clean. Read-only inspection (`git log --all --oneline -- ic_stock.db tmu_grades.db .env tools/lore.tmu_files.json tools/lore.json`) confirms **6 historical commits** (`00ecf89`, `3dfa0fe`, `a74e000`, `ac792f8`, `5674035`, `1365c0b`) contain historical blobs for removed database or private server lore files.  
  **Owner action required:** Decide explicitly whether to (a) scrub those paths from history (e.g., `git filter-repo` or exporting a fresh public history) before toggling repository visibility to Public, or (b) record explicit written acceptance of historical exposure if the repo remains private or history is already scrubbed.
  - *Inspection command:* `git log --all --stat -- ic_stock.db tmu_grades.db .env tools/lore.tmu_files.json tools/lore.json`
- [ ] **BLOCKER 2 — Green GitHub Actions `Quality & Security Gate` across Python 3.11, 3.12, and 3.13 + `dependency-audit`:**  
  Confirm `ci.yml` passes all 3 offline `quality` matrix legs (`check_docs.py`, `check_command_docs.py`, `smoke_staging.py`, and branch coverage upload) and the networked `dependency-audit` (`pip-audit`) job.
  - *CI Run URL:* `<paste GitHub Actions run URL here>`
- [ ] **BLOCKER 3 — Host Docker Daemon Build & Mounted `/data` Container Smoke Test:**  
  Verify non-root UID `10001` writeability, `HEALTHCHECK` readiness, and restart persistence on a real host Docker daemon.
- [ ] **BLOCKER 4 — Railway Production Safeguards & Manual `DB_PATH` Cutover:**  
  Confirm Railway autodeploy is **OFF**, `replicas = 1`, persistent `/data` volume is mounted, pre-cutover online hot backup (`tools/backup_db.py`) is verified, and `DB_PATH=/data/campus.db` is set (see [`docs/deployment.md`](deployment.md#61-manual-railway-db_path-cutover-and-rollback)).
- [ ] **BLOCKER 5 — Manual Owner-Run Staging Verification & Redacted Screenshots:**  
  Execute [`docs/staging-runbook.md`](staging-runbook.md) using a dedicated non-production staging bot token against an isolated private test channel; complete the 11-family acceptance matrix and optionally capture redacted PNGs following [`docs/screenshots-guide.md`](screenshots-guide.md) before populating the `README.md` gallery.
