# Security Policy

## 1. Supported Versions

Only the current modernized release line receives security fixes and vulnerability updates.

| Version | Supported | Status |
|---|---|---|
| `1.0.0` (`1.0.x`) | :white_check_mark: | Active Supported Release |
| `0.2.x` | :x: | Unsupported (Upgrade to `1.0.0`) |
| `0.1.x` | :x: | Legacy Baseline (Do not deploy) |

---

## 2. Reporting a Vulnerability

If you discover a vulnerability, security defect, or accidental secret exposure:

1. **Do NOT open a public GitHub issue.**
2. Submit a private report via [GitHub Security Advisories](https://github.com/sukesan7/instigate-cafe-bot/security/advisories/new) or email **`imsukesan@gmail.com`**.
3. Include affected files or commands, offline reproduction steps, and potential impact.

Maintainers will review reports promptly and work on a fix or advisory before public disclosure.

---

## 3. Secret Disclosure & Incident Response

If a Discord bot token, Gemini API key, or third-party credential is accidentally exposed:

1. **Immediate Revocation:** Rotate or invalidate the credential immediately in its provider portal (Discord Developer Portal, Google AI Studio, Metrolinx Open Data, etc.).
2. **History Cleanup:** Remove the exposed artifact and rewrite Git history before making a private fork/repo public (see [Pre-Publication Security & History Cleanup](docs/deployment.md#91-pre-publication-security-and-history-remediation)).
3. **Environment Check:** Verify local and hosted environments do not retain exposed `.env` or backup files.

---

## 4. Privacy & Permission Boundaries

- **Ephemeral Resume Reviews:** `/ic resume` reads uploaded attachments in RAM only and never saves them to local disk, SQLite, or logs (though Discord CDN and opt-in Google Gemini API terms still apply).
- **No Chat History Persistence:** Personal Discord chat messages are not written to SQLite or log files.
- **User Erasure & Tombstoning:** Operators can delete a community member's profile and active game state via `user_store.delete_user_data(user_id, guild_id=..., anonymize_ledger=True)`, which hard-deletes active profile/game rows (`users`, `blackjack_users`, `fun_positions`), replaces direct user IDs on retained ledger, trade, and community attribution rows with per-row CSPRNG tombstones (`user_id = 0` without a reidentification map) to preserve aggregate guild supply math, and leaves honest disclosures around residual correlation surfaces, shared free-text content, operator backups, and external Discord/Gemini retention (see [docs/privacy.md](docs/privacy.md)).
- **Least-Privilege Permissions & Intents:** The bot requests minimal Discord permissions (bitmask `277025507392`) and **zero privileged intents by default** (covering all `/ic` slash commands, `/ic resume`, and direct `@mention` replies via Discord's mention-message exception). The privileged Message Content Intent is requested *only* when an operator explicitly enables both `LLM_ENABLED=true` and `LLM_CHANNEL_HISTORY_ENABLED=true` at process startup (requiring Developer Portal opt-in and a bot restart). CI checks run offline with zero Discord or hosting credentials.
- **Clean Contributions:** Never introduce real user data, private server lore, database files, or credentials into code, fixtures, screenshots, or issue reports.
