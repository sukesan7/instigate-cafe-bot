# Screenshots Capture & Redaction Guide

This guide defines the **manual owner-run procedure** for capturing clean, redacted Discord screenshots on a private staging server or isolated test channel once development is complete. Final gallery links in `README.md` remain **unreferenced** until the owner supplies and approves sanitized images (populated in final prompt E3).

> [!CAUTION]
> **Mandatory Isolation & Token Safety Rules**
> 1. **Dedicated staging application or isolated test channel + new non-production token:** Register a separate Discord Application (with a fresh non-production `DISCORD_TOKEN`) or run in a deliberately isolated private test channel with zero real member chat visible.
> 2. **Never run two bot copies with the same `DISCORD_TOKEN`:** Running two processes against the same token causes gateway session conflicts, duplicate responses, and duplicate poller broadcasts. Stop any active instance or use a separate staging token.
> 3. **Never upload actual student resumes:** For `/ic resume`, use only an empty/synthetic dummy file (`sample_resume.pdf`) and capture the consent barrier or offline checklist—never a real resume or real AI review containing personal data.
> 4. **Never screenshot personal IDs, tokens, or private records:** Crop or mask Discord snowflake IDs, real member names/avatars, server sidebars, `.env` files, API keys, private exams, and real balances.

---

## 1. Discord Desktop Capture Baseline

- **Discord Theme:** Standard Discord **Dark** theme (`100%` zoom, `16px` font scaling, window width `1000–1200 px`).
- **Bot Visual Theme:** Default **`instigate_cafe`** preset (`BOT_DISPLAY_NAME="InstigateAI"`, accent `#A64DFF` / `0xA64DFF`, footer `Instigate Cafe`) unless explicitly capturing an alternate theme comparison (`campus_generic`, `student_clean`, `toronto_tmu`, `emerald_academic`).
- **File Format:** Lossless `.png` under `assets/screenshots/`, EXIF/metadata stripped, ideally `< 500 KB` per image.

---

## 2. Prioritized Owner Shot List

Capture shots in priority order (Priorities 1–3 form the core README gallery; Priorities 4–5 are optional additions).

### Priority 1 — Onboarding & Command Discovery (`/ic getting-started` or `/ic help`)

#### Shot 1A: Interactive Onboarding Guide
- **Suggested Filename:** `assets/screenshots/01-getting-started.png`
- **Command:** `/ic getting-started`
- **Expected Embed Title:** `InstigateAI | Welcome & Daily Rewards`
- **Visual Theme:** `instigate_cafe` (accent `#A64DFF`, info/normal border)
- **Elements to Capture:** Slash-command header, embed title/subtitle badge (`Step 1 of 4`), welcome body, footer, and the `◀ Previous Step` / `Next Step ▶` buttons.
- **Safe Test Data:** None required (built-in static walkthrough text).
- **Crop Guidelines:** Tight crop around the bot reply block (~16 px margin). Exclude Discord channel sidebar, member list, and invoker personal avatar/username if non-generic.
- **Alt Text Draft:** `Discord embed showing the Instigate Cafe Bot /ic getting-started Step 1 of 4 onboarding guide with Previous Step and Next Step navigation buttons.`

#### Shot 1B: Dynamic Paginated Help Menu
- **Suggested Filename:** `assets/screenshots/02-help.png`
- **Command:** `/ic help` *(invoked by a standard non-admin test account)*
- **Expected Embed Title:** `InstigateAI | 📚 Main Utilities`
- **Visual Theme:** `instigate_cafe` (accent `#A64DFF`)
- **Elements to Capture:** Category title, command list with syntax examples, page footer, and `◀ Previous Page` / `Next Page ▶` buttons.
- **Safe Test Data:** Default `default` or `student_core` feature profile.
- **Crop Guidelines:** Tight crop to the bot embed + pagination row; hide server name, channel list, and user list.
- **Alt Text Draft:** `Discord embed displaying the Instigate Cafe Bot dynamic /ic help menu for Main Utilities with paginated navigation controls.`

---

### Priority 2 — Academics: Course Notes & Shared Exam Schedule (Synthetic Courses)

#### Shot 2A: Course Notes Lookup
- **Suggested Filename:** `assets/screenshots/03-notes.png`
- **Command:** `/ic notes course:CPS109` *(or a synthetic guild catalog course such as `CS101`)*
- **Expected Embed Title:** `📚 Course Notes — CPS109` *(or `📚 Course Notes — CS101`)*
- **Visual Theme:** `instigate_cafe` (accent `#A64DFF`)
- **Elements to Capture:** Course header, SSRF-validated study resource links, provenance/source attribution, and pagination buttons if multi-page.
- **Safe Test Data:** Public/synthetic course code (`CPS109` or synthetic guild catalog entry `CS101 — Sample Lecture Notes`). Never include private Google Drive links or personal student names beyond credited public note authors.
- **Crop Guidelines:** Crop strictly to the slash-command invocation and embed card (~16 px margin).
- **Alt Text Draft:** `Discord embed showing /ic notes results for a sample course with study guide links and source attribution.`

#### Shot 2B: Shared Guild Exam Schedule
- **Suggested Filename:** `assets/screenshots/04-exam-list.png`
- **Command:** `/ic exam list upcoming_only:True include_private:False`
- **Expected Embed Title:** `Exam Schedule` *(with subtitle `Timezone: America/Toronto`)*
- **Visual Theme:** `instigate_cafe` (accent `#A64DFF`)
- **Elements to Capture:** Exam list entries with course code, date/time, countdown timestamp, room placeholder, and pagination controls (if >1 page).
- **Safe Test Data:** 2–3 **synthetic** public/guild exams added specifically for the screenshot (e.g., `CS101 — Intro to Computing`, `MTH140 — Calculus I`, `Room: Hall A`, `visibility: guild`). Confirm **zero** `private` exams or real personal notes exist in the output.
- **Crop Guidelines:** Crop tightly to the embed + buttons; verify no personal usernames or real course section IDs appear.
- **Alt Text Draft:** `Discord embed showing /ic exam list with synthetic upcoming course exam dates, rooms, and America/Toronto timezone countdowns.`

---

### Priority 3 — Early-Career Job Board (Harmless ATS Fixtures / Sanitized Example)

#### Shot 3: Paginated Job Search
- **Suggested Filename:** `assets/screenshots/05-jobs.png`
- **Command:** `/ic jobs preset:tech_swe limit:5`
- **Expected Embed Title:** `💼 Campus Job Board • Page 1/N` *(or single-page header)*
- **Visual Theme:** `instigate_cafe` (accent `#A64DFF`)
- **Elements to Capture:** Page counter, 3–5 public/synthetic early-career internship listings showing employer, location, posted/indexed date metadata, HTTPS apply links, and pagination buttons.
- **Safe Test Data:** Public Greenhouse/Lever early-career job listings or locally seeded harmless ATS fixture postings (`Example Corp — Software Engineer Intern (Toronto, ON)`). Never include internal referral links or private career-portal credentials.
- **Crop Guidelines:** Tight crop to the embed and pagination bar; hide surrounding messages and channel sidebar.
- **Alt Text Draft:** `Discord embed showing the paginated /ic jobs board listing early-career software engineering internships with locations, dates, and HTTPS apply links.`

---

### Priority 4 (Optional) — User Profile, Virtual Stock Exchange, or Casino

#### Shot 4A: Virtual Stock Exchange (ICSE)
- **Suggested Filename:** `assets/screenshots/06-icse-market.png`
- **Command:** `/ic icse market`
- **Expected Embed Title:** `🧪 Instigate Cafe Stock Exchange`
- **Visual Theme:** `instigate_cafe` (accent `#A64DFF`)
- **Elements to Capture:** Default synthetic stock tickers, current virtual prices, weekly `%` deltas, and footer disclaimer.
- **Safe Test Data:** Run `/ic admin sync_catalog reset_to_default:True` on the staging database first so only default synthetic securities appear (never real student capstone team names or real member usernames).
- **Crop Guidelines:** Crop strictly to the market embed card.
- **Alt Text Draft:** `Discord embed displaying the virtual Instigate Cafe Stock Exchange (/ic icse market) with synthetic ticker prices and weekly percentage changes.`

#### Shot 4B: User Profile or Blackjack Hand *(Optional Alternative)*
- **Suggested Filename:** `assets/screenshots/07-profile-or-casino.png`
- **Command:** `/ic profile` *(using a clean test account)* or `/ic casino blackjack bet:50`
- **Expected Embed Title:** `👤 <Test Display Name>` *(profile)* or interactive Blackjack hand embed with `Hit`, `Stand`, `Double Down`, `Split` buttons.
- **Visual Theme:** `instigate_cafe` (accent `#A64DFF`, semantic green/red status on settled hands)
- **Elements to Capture:** Virtual balance, portfolio summary or card hand + action buttons.
- **Safe Test Data:** Clean staging test account with synthetic virtual balance (e.g., `$1,000`). Mask or avoid any real personal Discord handle or avatar.
- **Crop Guidelines:** Tight crop to embed (+ buttons for Blackjack); mask personal display name/avatar if needed.
- **Alt Text Draft:** `Discord embed showing a test account's virtual campus economy profile and interactive card game controls.`

---

### Priority 5 (Optional) — Administration Setup & Redacted System Status

#### Shot 5A: Admin Setup Dashboard *(Private Values Masked)*
- **Suggested Filename:** `assets/screenshots/08-admin-setup.png`
- **Command:** `/ic admin setup` *(ephemeral)*
- **Expected Embed Title:** `⚙️ Configuration Dashboard — <Masked Server Name>`
- **Visual Theme:** `instigate_cafe` (accent `#A64DFF`)
- **Elements to Capture:** Feature profile status, setup buttons, and channel configuration overview.
- **Safe Test Data:** Isolated staging server named e.g. `Campus Bot Staging` with generic channel names (`#bot-commands`, `#job-alerts`).
- **Crop Guidelines:** Mask or solid-fill any real server name, private channel names, channel snowflake IDs, and role IDs before saving.
- **Alt Text Draft:** `Ephemeral Discord embed showing the /ic admin setup configuration dashboard with server profile and channel controls.`

#### Shot 5B: Redacted System Status *(Private Values Masked)*
- **Suggested Filename:** `assets/screenshots/09-status.png`
- **Command:** `/ic status` *(ephemeral)*
- **Expected Embed Title:** `📊 System Status — <Masked Server Name>`
- **Visual Theme:** `instigate_cafe` (accent `#A64DFF` or semantic `success`/`warning` badge)
- **Elements to Capture:** Module readiness summary (`core`, `academics`, `careers`, `economy`), Schema v7 WAL status, and background task states.
- **Safe Test Data:** Staging server with non-production token and synthetic guild/channel names.
- **Crop Guidelines:** Verify **zero** tokens, API keys, host filesystem paths, or private guild/channel snowflake IDs appear anywhere in the frame.
- **Alt Text Draft:** `Ephemeral Discord embed showing /ic status diagnostics with module readiness badges and SQLite WAL schema health.`

#### Optional Bonus Shot: Ephemeral AI Resume Consent Barrier *(Never Real Resumes)*
- **Suggested Filename:** `assets/screenshots/10-resume-consent.png`
- **Command:** `/ic resume file:sample_resume.pdf discipline:Software Engineering & Tech (SWE)`
- **Expected Embed Title:** `📄 InstigateAI Resume AI Review — Privacy Notice`
- **Visual Theme:** `instigate_cafe` (semantic `warning` / consent prompt)
- **Elements to Capture:** Privacy disclosure explaining in-memory processing + Discord/Gemini boundaries and the `Submit for AI Review` / `View No-AI Checklist` / `Cancel` buttons.
- **Safe Test Data:** Empty 1-page dummy `sample_resume.pdf` only; click **Cancel** or **View No-AI Checklist**—never upload or screenshot a real student resume.
- **Crop Guidelines:** Tight crop to ephemeral consent embed and button row.
- **Alt Text Draft:** `Ephemeral Discord privacy consent embed for /ic resume offering Submit for AI Review, View No-AI Checklist, and Cancel buttons.`

---

## 3. Pre-Commit Verification & Future Gallery Template (E3)

Do **not** reference `assets/screenshots/*.png` in `README.md` until the sanitized files exist on disk and pass offline checks:

```bash
uv run python tools/scan_secrets.py --enforce-clean
uv run python tools/check_docs.py
```

Once approved PNGs are added under `assets/screenshots/` in final prompt E3, replace the text note in `README.md`'s `Screenshots / Gallery` section with verified Markdown image links using the alt-text drafts above.
