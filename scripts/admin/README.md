# Administrative Maintenance Scripts (`scripts/admin/`)

> [!CAUTION]
> Scripts in this directory perform **destructive Discord API maintenance operations** (such as clearing registered slash commands) and are **never** invoked by CI, `Makefile`, or automated tests.

## Available Scripts

- **`purge_global_commands.py`** — Clears registered Discord slash commands for either a specific guild (`--scope guild --guild-id <ID>`) or globally (`--scope global`).
  - **Safe by default:** Defaults to `--dry-run`, makes zero network requests, and never reads `DISCORD_TOKEN` implicitly.
  - **Explicit opt-in for live mutation:** Requires `--execute`, `--token-env <ENV_VAR_NAME>` (never hardcoded), and either interactive confirmation or `--confirm --confirm-phrase "<EXPECTED_PHRASE>"`.
  - **Target echo & redaction:** Echoes the exact target application/account and endpoint before mutating, and redacts any token material from error output.

### Usage Examples

```bash
# 1. Safe offline dry-run preview (default; zero network, zero token read)
uv run python scripts/admin/purge_global_commands.py --scope guild --guild-id 900000000000000001

# 2. Dry-run preview for global command purge
uv run python scripts/admin/purge_global_commands.py --scope global

# 3. Live execution (manual owner action only, using an explicit staging env var)
STAGING_BOT_TOKEN="<your-staging-token>" uv run python scripts/admin/purge_global_commands.py \
  --scope guild --guild-id 900000000000000001 \
  --execute --token-env STAGING_BOT_TOKEN \
  --confirm --confirm-phrase "PURGE GUILD 900000000000000001"
```
