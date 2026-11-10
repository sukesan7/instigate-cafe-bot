# Instigate Cafe Bot — Tooling, Packaging & Quality Gate Baseline

**Phase 2C: Tooling and Source-Level Quality Gate**  
**Baseline Date:** November 9, 2026  
**Scope:** Reproducible dependency management, local/CI offline quality gates, targeted test formatting, and non-destructive baseline characterization of the 11,240-line runtime codebase (`icbot/` and `main.py`).

---

## 1. Canonical Packaging Workflow & Python Support Matrix

### 1.1 Packaging Strategy (`uv` Canonical + Pip Lockfile)

| Artifact | Role | Canonical? | Notes |
| :--- | :--- | :--- | :--- |
| [`pyproject.toml`](../pyproject.toml) | Source of truth for project metadata, runtime deps, `[dependency-groups] dev`, pytest, coverage, Ruff, and Mypy rules | **Yes** | Sets `requires-python = ">=3.10,<3.14"` and `[tool.uv] package = false` (application, not a library wheel). |
| [`uv.lock`](../uv.lock) | Cross-platform locked dependency graph (**93** resolved packages across runtime + dev) | **Yes** | Verified with `uv lock --check` and installed cleanly with `uv sync --frozen`. |
| [`requirements.lock`](../requirements.lock) | Hash-pinned runtime-only export (`uv export --no-dev --no-emit-project -o requirements.lock`) | Opt-in / Audit | Used by `pip-audit -r requirements.lock` and non-`uv` `pip install --require-hashes` / future Railway deploys. |
| [`requirements.txt`](../requirements.txt) | Legacy unpinned dependency list | Retained for compatibility | Kept intact for existing Nixpacks/Railway assumptions until Phase 5/6 deployment modernization. |

**Common local setup commands:**

```bash
# Provision Python 3.12 virtualenv and install locked runtime + dev tools
uv venv --python 3.12 .venv
uv sync --frozen

# Re-export hashed runtime lock after dependency edits in pyproject.toml
uv lock
uv export --no-dev --no-emit-project -o requirements.lock
```

### 1.2 Python Version Support Matrix

| Python Version | Status in Metadata | CI Matrix (`.github/workflows/ci.yml`) | Notes & Caveats |
| :--- | :--- | :--- | :--- |
| **< 3.10** | Unsupported | Not run | Excluded by `requires-python = ">=3.10,<3.14"`. |
| **3.10** | Minimum supported floor | Metadata floor only | Not executed in default CI matrix; union syntax uses `from __future__ import annotations` where present, while legacy modules still contain `typing.Optional` / `Dict` / `List`. |
| **3.11** | **Supported & Tested** | Tested (`"3.11"`) | Matches Ruff `target-version = "py311"`. Primary Railway/Linux target. |
| **3.12** | **Primary Local & Tested** | Tested (`"3.12"`) | Local `.venv` interpreter (`Python 3.12.13`), `[tool.mypy] python_version = "3.12"`. |
| **3.13** | **Supported & Tested** | Tested (`"3.13"`) | Requires `audioop-lts>=0.2.2; python_version >= '3.13'` (declared in `pyproject.toml` and locked in `uv.lock`) because stdlib `audioop` imported by `discord/player.py:30` was removed in 3.13. |
| **3.14** | Excluded (`<3.14`) | Not run | Garuda host `/usr/bin/python3` is `3.14.9`; native C-extension wheels (`numpy`, `matplotlib`, `aiohttp`) lag pre-release CPython ABIs. |

### 1.3 Disposable Virtual Environment Determinism Proof

Verified deterministic runtime installation into an isolated disposable environment outside `.venv`:

```bash
uv venv --python 3.12 /tmp/campus-bot-verify-venv
UV_PROJECT_ENVIRONMENT=/tmp/campus-bot-verify-venv uv sync --locked --no-dev
/tmp/campus-bot-verify-venv/bin/python tools/smoke_offline.py
rm -rf /tmp/campus-bot-verify-venv
```

- **Result:** Resolved **93** locked graph packages (`1ms`), installed **27** runtime-only packages (`65ms`, `discord-py==2.7.1`, `aiohttp==3.14.5`, `aiosqlite==0.22.1`, `matplotlib==3.11.2`, `numpy==2.5.4`), and passed `tools/smoke_offline.py` (`80` AST-checked files, `17` feature modules, `46` `/ic` commands registered, `5` example lore entries loaded, zero network/gateway activity) before immediate cleanup.

---

## 2. Quality Gates, Makefile, Pre-commit & Offline CI

### 2.1 Operational Safety Tiers (`Makefile`)

All developer workflows are codified in [`Makefile`](../Makefile) with explicit `SAFE_ENV` overrides clearing secrets and disabling background pollers (`DISCORD_TOKEN=""`, `LLM_ENABLED="false"`, `MARKET_WATCH_ENABLED="false"`, `BTC_WATCH_ENABLED="false"`, `JOBS_POLL_SECONDS="0"`):

| Tier | Targets | Scope & Safety Boundary |
| :--- | :--- | :--- |
| **Tier 1 (Static)** | `make format`, `make format-check`, `make lint`, `make lint-baseline`, `make typecheck`, `make typecheck-baseline` | Zero runtime execution, zero network. Enforced lint/format targets are strictly scoped to `tests/`, `scripts/manual/`, [`tools/check_syntax.py`](../tools/check_syntax.py), and [`tools/smoke_offline.py`](../tools/smoke_offline.py) during Phase 2. |
| **Tier 2 (Offline)** | `make smoke-offline`, `make test`, `make test-cov`, `make precommit`, `make check` | Runs with cleared tokens, neutralized `dotenv.load_dotenv`, and blocked outbound IPv4/IPv6 sockets (`tests/conftest.py` and [`tools/smoke_offline.py`](../tools/smoke_offline.py)). |
| **Tier 3 (PyPI Only)** | `make audit` | Runs `pip-audit -r requirements.lock` against PyPI OSV advisory endpoints. Never connects to Discord, Railway, or external bot providers. |
| **Tier 4 (Forbidden)** | `python main.py`, `tools/purge_global_commands.py`, `git push` | Not exposed in `Makefile`, `.pre-commit-config.yaml`, or CI. Requires explicit owner approval in later deployment phases. |

### 2.2 Pre-commit Safeguards (`.pre-commit-config.yaml`)

[`.pre-commit-config.yaml`](../.pre-commit-config.yaml) uses **`repo: local` hooks** so hooks execute 100% offline with pinned `.venv` tools, never clone remote hook repos in sandboxed runs, and never touch the 11,240 lines of legacy `icbot/`/`main.py` formatting in Phase 2:

1. `block-forbidden-files` (`language: fail`): Blocks commits staging `.env*` (except `.env.example`), `*.db` / `-wal` / `-shm`, `*.sqlite*`, `*.pem`, `*.key`, `id_rsa*`, `id_ed25519*`, or private `lore.*.json` packs (allowing `tools/lore.example.json` and `tests/fixtures/synthetic_lore.json`).
2. `scan-secrets-and-hygiene` (`python3 tools/scan_secrets.py --enforce-clean`): Verifies `.env.example` hygiene, secret regexes, and zero tracked forbidden artifacts (`109` tracked files scanned).
3. `block-private-key-blocks` & `check-merge-conflicts` (`language: pygrep`): Rejects PEM private key headers and unresolved Git conflict markers.
4. `check-syntax-ast-json-toml-yaml` ([`tools/check_syntax.py`](../tools/check_syntax.py)): Validates syntax of staged `.py`, `.json`, `.toml`, and `.yml`/`.yaml` files.
5. `ruff-check-scoped` & `ruff-format-check-scoped`: Enforces Ruff lint and formatting on modernized `tests/`, `scripts/manual/`, [`tools/check_syntax.py`](../tools/check_syntax.py), and [`tools/smoke_offline.py`](../tools/smoke_offline.py).

### 2.3 Offline GitHub Actions CI ([`.github/workflows/ci.yml`](../.github/workflows/ci.yml))

- Minimal `contents: read` permissions; matrix across Python `["3.11", "3.12", "3.13"]` via `astral-sh/setup-uv@v5`.
- Hardcodes blank tokens and disabled pollers at workflow level.
- Executes: `uv lock --check` → `uv sync --frozen --dev` → `tools/scan_secrets.py --enforce-clean` → `pre-commit validate-config` + `pre-commit run --all-files` → scoped `ruff check` + `ruff format --check` → `make typecheck` → `tools/smoke_offline.py` → `pytest --cov=icbot --cov-report=term-missing -v`.
- Contains **zero** deployment steps, Railway tokens, Discord tokens, or live network probes.

---

## 3. Targeted Safe Formatting & Before/After Test Comparison

Formatting (`make format`) was applied **only** to `tests/`, `scripts/manual/`, [`tools/check_syntax.py`](../tools/check_syntax.py), and [`tools/smoke_offline.py`](../tools/smoke_offline.py). `git diff --name-only icbot/ main.py` confirmed **0** modified files in runtime bot code (`11,240` LOC untouched).

| Check | Before Formatting | After Formatting (`tests/` + `scripts/manual/`) |
| :--- | :--- | :--- |
| **Scoped `ruff format --check`** | `18 files would be reformatted, 5 files already formatted` | `25 files already formatted` (incl. 2 new `tools/` helpers) |
| **Scoped `ruff check`** | `1` finding (`SIM300` false positive in `tests/unit/test_game_invariants.py:182`, now in `ignore`) | `All checks passed!` (`0` errors) |
| **Offline `pytest` suite** | `204 passed, 19 xfailed, 4 warnings in 2.09s` | `204 passed, 19 xfailed, 4 warnings in 2.18s` |

---

## 4. Baseline Static Analysis Characterization (Untouched `icbot/`, `main.py`, `tools/`)

### 4.1 Ruff Baseline Summary (`446` Lint Findings, `39` Unformatted Files)

Measured via `make lint-baseline` (`ruff check icbot main.py tools --statistics`): **`446` findings** under [`pyproject.toml`](../pyproject.toml) rules (or **`456`** including the `10` intentionally ignored `RUF001` Unicode symbol warnings; `206` auto-fixable, `36` unsafe auto-fixes), plus `39` unformatted files (`19` already formatted across `58` total files):

1. **Correctness & Bug Signals (Priority Phase 3/4 fixes):**
   - `F841` (`3`): Unused local variables (e.g., `icbot/features/jobs.py:115` `blob`, `icbot/services/jobs_ats_client.py:93,167`).
   - `RUF059` (`1`): Unused unpacked `is_live` variable (`icbot/features/blackjack.py:297`).
   - `DTZ003` (`5`), `DTZ901` (`2`), `DTZ007` (`1`): Deprecated naive `datetime.utcnow()` / naive min/max / strptime without timezone (`exams.py:221` actively emits runtime `DeprecationWarning` in `pytest`, `report.py:18`, `market_daily_report.py:210`, `main.py:244`).
   - `E701` (`3`), `E741` (`1`): Single-line `if ...: return` colons and ambiguous `l` loop variable (`icbot/features/grades.py`).
2. **Error Suppression & Fragile Control Flow:**
   - `BLE001` (`117`): Blind `except Exception:` blocks across `config.py`, features, services, pollers, and `main.py`.
   - `S110` (`12`) & `S112` (`9`): Silent `try ... except: pass` / `continue` swallowing errors without logging.
   - `B904` (`5`): Raising inside `except` without `from err`.
   - `B905` (`5`): Unbounded `zip()` without `strict=`.
3. **Readability & Python 3.11+ Modernization (`280+` mechanical findings):**
   - `UP006` (`62`), `UP045` (`47`), `I001` (`32`), `UP035` (`30`), `F401` (`24`), `SIM117` (`16`), `UP017` (`9`), `B010` (`8`), `RUF046` (`7`), `G004` (`6`), `SIM103` (`5`), `UP037` (`5`), `SIM105` (`5`), `SIM102` (`4`), `SIM108` (`4`), `UP041` (`3`), `B007` (`3`), `UP015` (`2`), `RUF005` (`2`), `F541` (`1`), `G201` (`1`), `PIE810` (`1`), `UP007` (`1`), `PIE790` (`1`), `PIE808` (`1`), `RUF022` (`1`), `EXE001` (`1` on `tools/scan_secrets.py`).
4. **Intentional Compatibility & Exclusions (Recorded):**
   - `RUF001`: Excluded because Discord UI strings intentionally use Unicode typography (`–` en-dashes, `♠` suit symbols).
   - `SIM300`: Excluded due to false-positive Yoda warnings on readable symmetric set invariants (`rl.RED_NUMBERS | rl.BLACK_NUMBERS == set(range(1, 37))`).
   - `tests/*` ignores `S101` for pytest `assert` statements.

### 4.2 Mypy Incremental Gate & Baseline (`47` Errors in `13` Files across `58` Files)

#### Incremental Enforced Gate (`make typecheck` — **0 errors** across **22** files)
Checked with `check_untyped_defs = true`, `warn_return_any = true`, `warn_redundant_casts = true`, and `no_implicit_optional = true`:
- `icbot/config.py`, `icbot/permissions.py`, `icbot/embeds.py`, `icbot/app.py`, `icbot/utils.py`
- `icbot/games/blackjack_engine.py`, `icbot/games/roulette_engine.py`, `icbot/games/slots_engine.py`
- `icbot/services/{user_service,fun_market_catalog,gemini_client,sports_client,btc_esplora,crypto_fng,jobs_ats_client,ttc_client,yahoo_quotes}.py`
- `tools/{check_syntax,lore_loader,scan_secrets,smoke_offline}.py`

*(Additional clean modules like `icbot/features/help.py` pass base mypy cleanly, while those importing `fun_market.py`, `go_client.py`, or `job_alerts.py` pull in un-remediated transitive type errors until Phase 3/4.)*

#### Baseline Errors (`make typecheck-baseline` — **47** errors across **13** files)

| Category | File(s) | Count | Root Cause / Remediation Target |
| :--- | :--- | :---: | :--- |
| **Runtime Call Bug** | `icbot/features/grades.py` (`191,200,227`) | 3+4 | Unexpected `is_error=True` kwarg passed to `embeds.system()` (`ISSUE-2B-08`) + `Settings | None` guard. |
| **View Union / `None` Message** | `icbot/features/blackjack.py` (`305,391,392,583,615,657,696`) | 7 | `BlackjackTerminalView` assigned to `BlackjackView` variable; missing `message is not None` guard before `.edit()`. |
| **aiohttp `ClientTimeout` & `URL`** | `jobs_workday_client.py` (`117,131`), `go_client.py` (`113`), `prof.py` (`79`), `notes.py` (`32,50`) | 6 | Passing raw `int` to `timeout=` instead of `aiohttp.ClientTimeout(total=...)`; passing `str` to `filter_cookies()` instead of `yarl.URL` (`131`). |
| **`asyncio.gather` Error Handling** | `icbot/features/notes.py` (`113`) | 1 | Assignment from `gather(..., return_exceptions=True)` union includes `BaseException`. |
| **SQLite `Row` & Return Annotations** | `icbot/db.py` (`253,258`), `icbot/services/fun_market.py` (`114,452,559`) | 5 | `aiosqlite.Row` vs `tuple[Any, ...]` signatures; `PositionRow | None` returned where non-optional annotated. |
| **Workday Job Type Mismatch** | `icbot/tasks/jobs_workday_alerts.py` (`157,163,184,196` + `181,226`) | 10 | Passing `list[JobItem]` into `JobPosting` functions + unnarrowed channel `.send()`. |
| **Discord Channel Narrowing** | `icbot/tasks/job_alerts.py` (`516,559`) | 6 | Missing `isinstance(ch, discord.abc.Messageable)` check before `ch.send(...)`. |
| **`None` Settings / CommandTree** | `icbot/features/admin.py` (`9,26`), `icbot/features/info.py` (`41,79`), `main.py` (`94`) | 5 | `settings` module globals typed `Any | None`; `CommandTree.interaction_check` `Interaction[Client]` vs `Interaction[Bot]`. |

---

## 5. Dependency Vulnerability Audit & Honest Limitations

- **`pip-audit -r requirements.lock`** (27 pinned runtime deps): **`No known vulnerabilities found`** (exit code 0, checked November 9, 2026).
- **`pip-audit --local`** (93 resolved `.venv` packages incl. dev tooling): **`No known vulnerabilities found`** (exit code 0).
- **Documented Limitations:**
  1. `make audit` requires PyPI network access (`Tier 3`) and is omitted from the offline `make check` CI gate to avoid flaky CI failures on PyPI API downtime.
  2. Live Discord gateway connectivity, Railway container builds, Python `3.10` / `3.14` CI execution, and unmocked third-party provider APIs are **not** executed by automated checks.
  3. Formatting and Ruff/Mypy remediation for the `11,240`-line legacy runtime source (`icbot/`, `main.py`) and non-helper tools (`tools/lore_loader.py`, `tools/purge_global_commands.py`, `tools/scan_secrets.py`) remain intentionally deferred to Phases 3 and 4 so bug-fix diffs stay clean and reviewable.

