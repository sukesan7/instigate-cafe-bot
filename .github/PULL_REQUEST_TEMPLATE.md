## Description
Brief summary of the proposed changes and their operational intent.

## Related Issue / Prompt
Fixes #(issue number) or relates to Phase XX modernization.

## Subsystem Impact
- [ ] Academics (`icbot/academics/`)
- [ ] Careers / ATS Ingestion (`icbot/tasks/jobs*`, `icbot/features/jobs.py`)
- [ ] Casino / Economy (`icbot/games/`, `icbot/services/casino*`)
- [ ] Fun-Market ICSE (`icbot/features/icse.py`, `icbot/storage/market.py`)
- [ ] Administration & Setup (`icbot/features/admin.py`)
- [ ] Tooling / Infrastructure / Documentation

## Offline Quality Gate Checklist
Before requesting review, confirm that all local offline checks pass:
- [ ] `uv run pytest` passes 100% of tests offline.
- [ ] `uv run ruff check . && uv run ruff format --check .` reports clean formatting.
- [ ] `uv run mypy .` reports no type errors.
- [ ] `uv run python tools/smoke_offline.py` passes zero-token import check.
- [ ] `uv run python tools/scan_secrets.py --enforce-clean` reports zero leaked secrets.

## Security & Privacy Verification
- [ ] No Discord bot tokens, Gemini keys, or credentials are committed.
- [ ] No SQLite database files (`*.db`), WAL journals, or `.env` files are tracked in git.
- [ ] No unmocked outbound network requests are executed during imports or unit tests.
- [ ] Ephemeral data handling invariants are preserved (e.g. resumes never persisted).

