# Manual Live Diagnostic Scripts (`scripts/manual/`)

> [!WARNING]
> All scripts in this directory are **manual, live network diagnostics**.
> They are intentionally excluded from `pytest` discovery (`norecursedirs = ["scripts", ...]` in `pyproject.toml`) because they contact external websites or write local cache databases when executed directly.
>
> **Do not run these scripts in offline CI or sandboxed verification.**

## Relocated Legacy Probes
- `test_kh_notes.py` — Live HTML link extraction diagnostic for configured course notes HTML pages.
- `test_notes.py` — Live SPA JS bundle link extraction diagnostic for Vite/React course notes portals.
- `test_rmp.py` — Live RateMyProfessors GraphQL API diagnostic.
- `test_tmu.py` — Live TMU Grades scraper + Matplotlib chart generation diagnostic.
- `test_yf.py` — Live Yahoo Finance quote & cookie+crumb session diagnostic.

Every module wraps network execution inside `main()` guarded by `if __name__ == "__main__":` so importing any module inspects cleanly with zero network side effects.

