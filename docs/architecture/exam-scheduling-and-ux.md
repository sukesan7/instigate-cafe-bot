# Exam Scheduling & Student UX Architecture

## 1. Exam Record Contract

Each exam row in SQLite (`exams`) is modeled by `icbot.academics.models.Exam` and strictly scoped to a guild (`guild_id`):

| Column / Field | Type | Default | Purpose |
| :--- | :--- | :--- | :--- |
| `id` | `INTEGER PRIMARY KEY` | auto | Unique row ID returned inside the same `BEGIN IMMEDIATE` transaction |
| `guild_id` | `INTEGER` | `0` | Server isolation boundary |
| `course_code` | `TEXT` | required | Display code (e.g., `COE817`, `CS 135`, `HIST-2010`) |
| `course_key` | `TEXT` | normalized | Canonical uppercase alphanumeric key (`COE817`, `CS135`, `HIST2010`) |
| `course_name` | `TEXT` | required | Course or assessment title |
| `start_ts` | `INTEGER` | required | Timezone-aware Unix epoch timestamp (UTC seconds) |
| `end_ts` | `INTEGER \| NULL` | `NULL` | Optional end Unix epoch timestamp (`>= start_ts`) |
| `is_all_day` | `INTEGER` (bool) | `0` | Optional all-day event anchored to local midnight |
| `term` | `TEXT` | `''` | Optional academic term label (e.g., `Fall 2026`, `Winter 2027`) |
| `organizer` | `TEXT` | `''` | Optional instructor, department, or student group |
| `location` | `TEXT` | `''` | Room, building, or online instructions |
| `notes` | `TEXT` | `''` | Allowed materials, seating, or exam instructions |
| `visibility` | `TEXT` | `'guild'` | `'guild'`, `'public'`, or `'private'` (private visible only to creator/admins) |
| `source` | `TEXT` | `'manual'` | `'manual'`, `'csv_import'`, or `'ics_import'` |
| `created_by` | `INTEGER` | `0` | Creator Discord user snowflake |
| `channel_id` | `INTEGER` | `0` | Channel snowflake where exam was created/imported |
| `external_uid` | `TEXT` | `''` | Deduplication UID for idempotent CSV/ICS re-imports |
| `created_at` | `INTEGER` | required | Creation Unix epoch timestamp |

---

## 2. Command Inputs, Outputs, and Authorization Matrix

| Command | Authorization | Key Inputs | Behavior & Output |
| :--- | :--- | :--- | :--- |
| `/ic exam list` | Any guild member | `course` (autocomplete), `term`, `upcoming_only`, `include_private` (default `True`) | Upcoming-first sorted schedule; hides `private` exams from non-creator/non-admin viewers; decides `ephemeral` **before** `interaction.response.defer` (`ephemeral=True` whenever any `private` exam is included, `ephemeral=False` for public/guild-only schedules or when `include_private=False`); paginates via actor-bound `ExamPaginationView` (>10 exams). |
| `/ic exam add` | Admin or `ALLOWED_ROLE_IDS` | `course_code` (autocomplete), `course_name`, `date`, `start_time`, `end_time`, `location`, `notes`, `organizer`, `visibility`, `term`, `all_day`, `dst_resolution` | Same-transaction `BEGIN IMMEDIATE` insert returning exact row ID; rejects duplicates, leap-day errors, and DST gaps/unresolved overlaps; emits non-blocking overlap notice if another visible timed exam overlaps. Always `ephemeral=True`. |
| `/ic exam remove` | Admin or `ALLOWED_ROLE_IDS` | `exam_id` | Atomic guild-scoped lookup + delete; rejects nonexistent or cross-guild IDs without mutating other guilds. Always `ephemeral=True`. |
| `/ic exam clear` | Admin or `ALLOWED_ROLE_IDS` | `term` (optional), `preserve_manual` (optional) | Presents ephemeral `ExamClearConfirmView` destructive confirmation barrier bound to the initiating admin & guild; removes nothing until **Confirm Clear** is clicked. Always `ephemeral=True`. |
| `/ic exam import` | Admin or `ALLOWED_ROLE_IDS` | `fmt` (`csv`/`ics`), `file` or `data`, `dst_resolution` | Offline sandboxed parser; preserves existing manual exams; deduplicates repeat imports by `external_uid` and `(course_key, start_ts, is_all_day)`. Always `ephemeral=True`. |
| `/ic exam export` | Any guild member | `fmt` (`ics`/`csv`), `course`, `term`, `upcoming_only`, `include_private` (default `False`) | Exports visible exams as an ephemeral Discord file attachment (`exams_<guild_id>.ics` or `.csv`); excludes `private` exams by default (`include_private=False`) so exported files are safe to share publicly; neutralizes spreadsheet formula prefixes on CSV export. |

---

## 3. Timezone & DST Validation Semantics (`icbot/academics/exams_service.py`)

- **Strict Calendar & Leap Days**: Validated through Python `datetime(..., tzinfo=ZoneInfo(tz_name))`. `2028-02-29` is accepted; `2026-02-29` is rejected.
- **DST Spring-Forward Gaps (Nonexistent Times)**: Verified via UTC round-trip (`dt0.astimezone(UTC).astimezone(tz)`). Wall times that do not exist in the campus timezone (e.g., `2026-03-08 02:30` in `America/Toronto`) are rejected with `ExamTimeValidationError`.
- **DST Fall-Back Overlaps (Ambiguous Times)**: Verified by comparing `fold=0` vs `fold=1` UTC offsets (e.g., `2026-11-01 01:30` EDT vs EST). Rejected by default (`dst_resolution="reject"`) with both offsets explained; explicitly resolvable via `dst_resolution="earlier"` (`fold=0`) or `"later"` (`fold=1`).
- **Discord Timestamps + Fallback**: Every schedule entry formats `<t:UNIX:F>` / `<t:UNIX:D>` / `<t:UNIX:R>` alongside a human-readable plain-text fallback with timezone abbreviation.

---

## 4. Sandboxed CSV & ICS Import / Export Subset

The bot **never** logs into university portals or fetches external calendar URLs during exam import.

### Sandbox Limits
- Maximum payload size: **128 KiB** (`MAX_IMPORT_BYTES`)
- Maximum line count: **500 lines** (`MAX_IMPORT_LINES`)
- Maximum events per import: **100 events** (`MAX_IMPORT_EVENTS`)
- NUL bytes (`\x00`), invalid UTF-8, nested `BEGIN:VEVENT`, and unclosed `VEVENT` blocks are rejected with `ExamImportError`.

### Documented CSV Columns
Required: `course_code,course_name,date` (`start_time` required unless `all_day=true`).  
Optional: `start_time,end_time,location,notes,term,organizer,all_day,visibility,uid`.

### Documented ICS Subset
`BEGIN:VCALENDAR` / `END:VCALENDAR` containing flat `BEGIN:VEVENT` / `END:VEVENT` entries with `UID`, `SUMMARY`, `DTSTART` (`VALUE=DATE`, UTC `Z`, or `TZID=...`), `DTEND`, `LOCATION`, `DESCRIPTION`, `ORGANIZER`, and `CATEGORIES` (mapped to `term`).

---

## 5. Visibility Classification, Response Audience & View Access Control

### Explicit Visibility Classification & Allowed Output Channels

| Visibility Tier | Authorized Viewers | Allowed Output Channels | Forbidden Output Channels |
| :--- | :--- | :--- | :--- |
| **`guild` / `public`** | All members of the owning `guild_id` (`interaction.guild.id == exam.guild_id`) | Public channel embeds (`/ic exam list`), guild-scoped CSV/ICS exports (`/ic exam export`), guild course autocomplete, opt-in admin-configured reminder channels (`compute_due_exam_reminders`) | Cross-guild interactions (`guild_b`), non-member or DM interactions (`interaction.guild is None`) |
| **`private`** | Exam creator (`exam.created_by > 0 and exam.created_by == viewer_id`) and authorized guild administrators (`is_admin_or_allowed`) within the same `guild_id` | **Ephemeral interaction responses only (`ephemeral=True`)** bound to the authorized viewer (`owner_id == interaction.user.id`, `guild_id == gid`), including ephemeral `/ic exam list`, ephemeral `ExamPaginationView`, and ephemeral `/ic exam export` when `include_private=True` | Public (non-ephemeral) `/ic exam list` embeds, public `ExamPaginationView`, default `/ic exam export` (`include_private=False`), public course autocomplete, channel reminder broadcasts (`compute_due_exam_reminders`), public error/status summaries |

### Response-Audience & View Security Invariants

1. **Pre-Defer Audience Resolution (`/ic exam list`)**: Because Discord does not allow changing an interaction's audience after `interaction.response.defer(...)`, `/ic exam list` validates the active guild/member boundary (`_require_active_guild_id`) and queries `ExamStore.list_exams(...)` **before** calling `interaction.response.defer`. Whenever any `visibility == 'private'` exam is present in the viewer's returned schedule, `/ic exam list` defers and sends with `ephemeral=True`. When no private exam is present (or when `include_private=False` is passed to deliberately share the public guild schedule), `/ic exam list` defers and sends with `ephemeral=False`.
2. **Defense-in-Depth Embed Filtering**: `build_exam_schedule_embed(..., allow_private=False)` strips any `visibility == 'private'` exam unless `allow_private=True` is explicitly provided for an ephemeral response.
3. **Safe Export Default (`/ic exam export`)**: Always defers and sends with `ephemeral=True`. By default (`include_private=False`), `exams_export_cmd`, `export_exams_csv`, and `export_exams_ics` exclude `private` exams even for admins/owners so exported `.ics`/`.csv` files are safe to share without accidentally leaking private sittings.
4. **Interactive View Authorization & Lifecycle (`ExamPaginationView`, `ExamClearConfirmView`)**:
   - Both views bind `owner_id` and `guild_id` and verify user ID, active guild membership, and timeout state on every button press (`interaction_check` and button callbacks).
   - Unauthorized or expired interactions are rejected ephemerally (`ephemeral=True`), even if the interaction was already deferred.
   - `on_timeout` disables all component buttons and safely edits `self.message`, suppressing `discord.HTTPException`, `discord.NotFound`, and `discord.Forbidden` if the message was deleted.

---

## 6. Opt-In Administrator Deadline Reminders

Exam reminders are **disabled by default** and never activate without explicit server administrator configuration:
- `EXAM_REMINDERS_ENABLED` / guild override `exam_reminders_enabled`: `false` by default.
- `EXAM_REMINDERS_CHANNEL_ID` / guild override `exam_reminders_channel_id`: `0` (disabled) by default.
- `EXAM_REMINDER_LEAD_HOURS` / guild override `exam_reminder_lead_hours`: defaults to `[24, 2]` (1–336 hours).
- `compute_due_exam_reminders` excludes `visibility='private'` exams (`include_private=False`) from channel broadcasts and deduplicates via `alert_delivery_dedupes` (`alert_type='exam_reminder'`) so restarts never duplicate alerts.

---

## 7. Legacy Migration Semantics (`0007_exam_scheduling_and_reminders`)

- Migration 7 is strictly additive (`ALTER TABLE exams ADD COLUMN ...` guarded by `PRAGMA table_info(exams)`).
- Pre-existing `/ic exam` rows automatically receive `term=''`, `is_all_day=0`, `source='manual'`, `channel_id=0`, and `external_uid=''`.
- Existing `/ic exam list`, `/ic exam add`, `/ic exam remove`, and `/ic exam clear` command names and RBAC checks are preserved.
- **Intentional Safety Change**: `/ic exam clear` now requires clicking **Confirm Clear** on an ephemeral confirmation prompt (`ExamClearConfirmView`) before deleting exams, preventing accidental single-command schedule wipes.

