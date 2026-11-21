# Screenshots Capture & Redaction Guide

This guide defines the **manual owner-run procedure** for capturing clean, redacted Discord screenshots once development is complete and the bot is running with a private `.env` on the Instigate Cafe private server (or a dedicated private test channel).

> [!IMPORTANT]
> **Safety Boundary:** Automated agents and CI workflows must **never** connect to Discord, load a private `.env`, or capture screenshots from a live server. Real screenshots are captured manually by the repository owner and wired into `README.md` during the final gallery step.

---

## 1. Environment & Channel Preparation

1. **Use a dedicated test channel** (e.g., `#bot-showcase` with no prior member chat visible) rather than an active community channel.
2. **Use synthetic or non-sensitive inputs only:**
   - Schedule a sample exam (e.g., `CPS109 — Midterm Exam`) with `visibility: public`—never capture private exams or personal notes.
   - Do **not** upload a real student resume PDF; for `/ic resume`, capture only the ephemeral `ResumeConfirmView` consent disclosure or the offline **No-AI Checklist** using a blank/dummy sample filename (e.g., `sample_resume.pdf`).
   - Avoid capturing real server member balances or real member usernames on `/ic leaderboard` or `/ic profile`.
3. **Discord Desktop Display Settings:**
   - Theme: **Dark** (standard Discord Dark theme)
   - Zoom: **100%** (`Ctrl+0`), Font Scaling: **16px** (default)
   - Window width: **1000–1200 px** so embeds render crisply without awkward line wrapping on GitHub desktop and mobile.

---

## 2. Recommended Command States to Capture

Save finalized, sanitized PNGs under `assets/screenshots/` using deterministic filenames:

| Filename | Command / State | What to Show | Special Privacy Check |
| :--- | :--- | :--- | :--- |
| `01-getting-started.png` | `/ic getting-started` | Welcome embed with step navigation buttons (`◀ Previous Step`, `Next Step ▶`) | Crop out invoker username/avatar if personal. |
| `02-help.png` | `/ic help` | Dynamic paginated help embed showing enabled student modules | Capture as a standard member (not exposing admin-only pages unless intended). |
| `03-exam-list.png` | `/ic exam list` | Shared guild exam countdown with `.ics` export guidance and pagination buttons | Confirm **zero** private exams or real student names appear. |
| `04-notes.png` | `/ic notes course:CPS109` | Academic notes embed showing course links and source attribution | Ensure no unrelated messages appear above/below. |
| `05-jobs.png` | `/ic jobs` | Paginated early-career job board showing HTTPS apply links and posted/indexed date metadata | Verify postings are public ATS listings only. |
| `06-resume-consent.png` | `/ic resume` *(with dummy file)* | Ephemeral `ResumeConfirmView` consent barrier (`Submit for AI Review` / `View No-AI Checklist` / `Cancel`) | **Never** capture real resume content, personal email, phone number, or real AI critique output. |
| `07-icse-market.png` | `/ic icse market` | Virtual stock exchange listings and weekly `%` changes | Use default synthetic catalog symbols (never private capstone team names). |
| `08-admin-setup.png` | `/ic admin setup` | Ephemeral setup wizard showing profile buttons and channel selectors | Blur/redact any private server channel names, role IDs, or guild IDs. |
| `09-status.png` | `/ic status` | Module readiness summary and Schema v7 WAL health | Confirm zero tokens, host paths, or private channel IDs are visible. |

---

## 3. Mandatory Cropping & Redaction Checklist

Before saving any image into `assets/screenshots/`:

1. **Crop tightly to the bot interaction block:** Include the slash-command header + bot embed + action buttons with ~16 px padding. **Exclude** the Discord server list, channel sidebar, member list, voice channels, and surrounding chat messages.
2. **Redact personal identifiers:** Blur or solid-fill any real Discord usernames, display names, custom nicknames, personal avatars, or snowflake IDs (`user_id`, `guild_id`, `channel_id`, `role_id`).
3. **Zero credentials or internal paths:** Never screenshot terminals running `.env`, `DISCORD_TOKEN`, `GEMINI_API_KEY`, `GO_API_KEY`, `ODDS_API_KEY`, Railway dashboards, or local filesystem paths.
4. **Strip image metadata & bound size:** Save as `.png`, strip EXIF/ancillary metadata if your capture tool embeds host info, and keep each image under ~500 KB.

---

## 4. Pre-Commit Verification & Gallery Template

After placing sanitized screenshots in `assets/screenshots/`, run the offline checks before updating `README.md`:

```bash
uv run python tools/scan_secrets.py --enforce-clean
uv run python tools/check_docs.py
```

When populating the `Screenshots / Gallery` section of `README.md` in the final polish step, link only verified files that exist on disk:

```markdown
| Academic Schedule | Early-Career Jobs |
| :---: | :---: |
| ![Exam List](assets/screenshots/03-exam-list.png) | ![Job Board](assets/screenshots/05-jobs.png) |

| AI Resume Consent Barrier | Virtual Stock Exchange (ICSE) |
| :---: | :---: |
| ![Resume Consent](assets/screenshots/06-resume-consent.png) | ![ICSE Market](assets/screenshots/07-icse-market.png) |
```
