# Manual Opt-In Live Network Probes (`scripts/manual/`)

> [!WARNING]
> All scripts in this directory are **opt-in, manual, live network probes**.
> They are named `probe_*.py` (retired from `test_*.py`) and excluded from `pytest` discovery (`norecursedirs = ["scripts", ...]` in `pyproject.toml`) so automated offline test suites never execute them accidentally.
>
> **Do not run these scripts in offline CI or sandboxed verification.**

## Available Manual Probes
- `probe_job_boards.py` — Opt-in live ATS verifier for ~100 curated Greenhouse, Lever, and Workday employer boards (supports `--include-existing` and `--write-env`).
- `probe_kh_notes.py` — Opt-in live HTML/Notion link extraction probe for configured course notes HTML pages.
- `probe_notes.py` — Opt-in live SPA JS bundle link extraction probe for Vite/React course notes portals.
- `probe_rmp.py` — Opt-in live RateMyProfessors GraphQL API connectivity probe.
- `probe_tmu.py` — Opt-in live TMU Grades HTML scraper + Matplotlib chart probe (writes local cache/chart when run directly).
- `probe_yf.py` — Opt-in live Yahoo Finance quote & cookie+crumb session probe.

Every script wraps network activity inside `main()` guarded by `if __name__ == "__main__":`, so importing any module remains offline-safe with zero network side effects.
