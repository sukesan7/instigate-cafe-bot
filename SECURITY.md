# Security Policy

## 1. Supported Versions

Only the current modernized release line is actively supported with security patches and vulnerability mitigations.

| Version | Supported | Status |
|---|---|---|
| `1.0.0-rc1` (`1.0.0rc1`) | :white_check_mark: | Active Supported Pre-Release |
| `0.2.x` | :x: | End of Support (Upgrade to `1.0.0-rc1`) |
| `0.1.x` | :x: | Unsupported / Legacy (Do not deploy) |

---

## 2. Reporting a Vulnerability

We take the security and privacy of our student community and contributors seriously. If you discover a potential vulnerability, security defect, or accidental secret exposure:

1. **Do NOT open a public GitHub issue.**
2. Submit a private report through [GitHub Security Advisories](https://github.com/sukesan7/instigate-cafe-bot/security/advisories/new) or contact the maintainer directly at **`imsukesan@gmail.com`**.
3. Include the following details to help us investigate:
   - Type of issue (e.g. secret leak, prompt injection, authorization bypass, SSRF, memory leak).
   - Component affected (file path, slash command, or background poller).
   - Reproduction steps or proof-of-concept (offline test or synthetic interactions only).
   - Potential impact on community privacy or service stability.

We will acknowledge receipt within **48 hours** and provide regular progress updates until the issue is resolved and patched.

---

## 3. Secret Disclosure and Incident Response

If a Discord bot token, Gemini API key, or third-party credential is accidentally committed or exposed:

1. **Immediate Revocation:** The compromised credential will be immediately invalidated and rotated at the provider dashboard (Discord Developer Portal, Google AI Studio, Metrolinx Open Data, etc.).
2. **History Remediation:** The affected commit or branch will be remediated in accordance with our [Pre-Publication Security & History Remediation Runbook](docs/deployment.md#91-pre-publication-security-and-history-remediation).
3. **Environment Audit:** Maintainers will verify all deployments to ensure uncredentialed environments are restored to offline defaults.

---

## 4. Privacy & Data Minimization Invariants

- **Ephemeral Resume Reviews:** Resumes submitted via `/ic resume` are parsed and evaluated strictly in memory. They are never written to disk, saved in SQLite, or logged.
- **Zero Raw Message Logging:** Personal chat messages are never recorded in application databases.
- **User Erasure & Tombstoning:** Operators can delete a community member's profile and active game state via `user_store.delete_user_data(user_id, guild_id=..., anonymize_ledger=True)`, which hard-deletes active profile/game rows (`users`, `blackjack_users`, `fun_positions`), replaces direct user IDs on retained ledger, trade, and community attribution rows with per-row CSPRNG tombstones (`user_id = 0` without a reidentification map) to preserve aggregate guild supply math, and leaves honest disclosures around residual correlation surfaces, shared free-text content, operator backups, and external Discord/Gemini retention (see [docs/privacy.md](docs/privacy.md)).
- **Least-Privilege Permissions & CI:** The bot requests minimal Discord permissions (bitmask `277025507392`) and **zero privileged intents by default** (covering all `/ic` slash commands, `/ic resume`, and direct `@mention` replies via Discord's mention-message exception). The privileged Message Content Intent is requested *only* when an operator explicitly enables both `LLM_ENABLED=true` and `LLM_CHANNEL_HISTORY_ENABLED=true` at process startup (requiring Developer Portal opt-in and a bot restart). CI workflows run exclusively offline with zero access to live Discord or hosting credentials.
- **Validated Attribution & Privacy:** Contributors must never introduce real user data, private server lore, or unlicensed third-party content into source, fixtures, or issue reports.

