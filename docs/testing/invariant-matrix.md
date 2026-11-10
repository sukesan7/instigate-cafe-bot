# Instigate Cafe Bot — Phase 2B Domain Invariant & Error-Handling Matrix

> Parent testing guide: [`docs/testing.md`](../testing.md)  
> Scope: Characterization of financial math, concurrency boundaries, Discord UI state transitions, poller checkpoints, external HTTP client error handling, and tracked legacy defects without premature modifications to `icbot/` or [`main.py`](../../main.py).

## Status Legend

| Status | Meaning |
| :--- | :--- |
| **PASS** | Invariant verified under offline execution (`pytest` deterministic mocks/SQLite). |
| **XFAIL (strict)** | Objectively incorrect runtime behavior reproduced offline and locked with `@pytest.mark.xfail(strict=True, reason="ISSUE-2B-XX: ...")` so tests fail fast if removed or unexpectedly changed before scheduled Phase 3/4 remediation. |

---

## 1. Domain Invariant Matrix

### A. Economy & Account Lifecycle ([`user_service.py`](../../icbot/services/user_service.py), [`blackjack.py`](../../icbot/features/blackjack.py))

| ID | Component | Invariant / Expected Contract | Test File & Node(s) | Status |
| :--- | :--- | :--- | :--- | :--- |
| `INV-ECON-01` | `UserStore.claim_daily` | Enforces exact `DAILY_COOLDOWN_SECONDS` (`86400s`): rejected at `+86399s` with `DailyAlreadyClaimedError` and unchanged balance; awards `+100` at `+86400s`. | [`test_economy_and_market_invariants.py`](../../tests/integration/test_economy_and_market_invariants.py) (`test_daily_cooldown_exact_second_boundaries`), [`test_storage_user_and_market.py`](../../tests/integration/test_storage_user_and_market.py) | **PASS** |
| `INV-ECON-02` | `UserStore.subtract_balance` | Concurrent debits on shared bankroll (`2 × $700` on `$1000`) serialize safely: exactly 1 succeeds, 1 raises `InsufficientBalanceError`, final balance `$300`. | [`test_economy_and_market_invariants.py`](../../tests/integration/test_economy_and_market_invariants.py) (`test_parallel_subtract_balance_prevents_overdraft_race`) | **XFAIL** (`ISSUE-2B-01`) |
| `INV-ECON-03` | `UserStore.claim_daily` | Concurrent daily claims on same user award reward at most once (`+100`, not `+200`). | [`test_economy_and_market_invariants.py`](../../tests/integration/test_economy_and_market_invariants.py) (`test_parallel_claim_daily_awards_exactly_once`) | **XFAIL** (`ISSUE-2B-02`) |
| `INV-ECON-04` | `UserStore` / `GameState.start` | Rejects `amount <= 0` (`0`, `-1`, `-500`) across `subtract_balance`, `add_balance`, and `GameState.start` with `ValueError` without altering balance. | [`test_economy_and_market_invariants.py`](../../tests/integration/test_economy_and_market_invariants.py) (`test_user_store_rejects_non_positive_amounts`), [`test_game_invariants.py`](../../tests/unit/test_game_invariants.py) (`test_blackjack_engine_rejects_non_positive_bet`) | **XFAIL** (`ISSUE-2B-03`) |
| `INV-ECON-05` | `UserStore.ensure_user` | Concurrent lazy user registration for the same unseen `user_id` is idempotent and never raises `sqlite3.IntegrityError`. | [`test_economy_and_market_invariants.py`](../../tests/integration/test_economy_and_market_invariants.py) (`test_parallel_ensure_user_registration_is_idempotent`) | **XFAIL** (`ISSUE-2B-04`) |
| `INV-ECON-06` | `BlackjackStore.apply_delta_and_stats` | Concurrent balance + stat settlements preserve cumulative `users.balance`, `blackjack_users.bankroll`, `wins`/`losses`, and `hands_played`. | [`test_economy_and_market_invariants.py`](../../tests/integration/test_economy_and_market_invariants.py) (`test_parallel_blackjack_apply_delta_preserves_all_updates`) | **XFAIL** (`ISSUE-2B-10`) |

### B. Fun Market (ICSE) Ledger & Risk Rules ([`fun_market.py`](../../icbot/services/fun_market.py))

| ID | Component | Invariant / Expected Contract | Test File & Node(s) | Status |
| :--- | :--- | :--- | :--- | :--- |
| `INV-MKT-01` | `buy_long` / `sell_long` / `portfolio` | Conserves net worth at entry (`cash + qty * price == initial_bankroll`), computes weighted-average cost (`(2*100 + 2*150)/4 == 125.0`), and tracks exact partial/full realized PnL. | [`test_economy_and_market_invariants.py`](../../tests/integration/test_economy_and_market_invariants.py) (`test_fun_market_weighted_average_net_worth_conservation_and_debt`) | **PASS** |
| `INV-MKT-02` | `open_short` / `cover_short` | Locks `math.ceil(price * qty * 1.5)` collateral while conserving net worth; releases proportional collateral on partial cover, exact remaining collateral on full close, and permits intentional negative cash debt under severe squeeze losses. | [`test_economy_and_market_invariants.py`](../../tests/integration/test_economy_and_market_invariants.py) (`test_fun_market_weighted_average_net_worth_conservation_and_debt`) | **PASS** |
| `INV-MKT-03` | `buy_long` / `sell_long` / `open_short` / `cover_short` | Rejects `qty <= 0` (`0`, `-1`, `-50`), inactive catalog symbols (`CONT`), and unknown symbols (`NOSUCH`) via `FunMarketError` with zero DB/balance/position mutation. | [`test_economy_and_market_invariants.py`](../../tests/integration/test_economy_and_market_invariants.py) (`test_fun_market_rejects_non_positive_quantities_without_mutation`) | **PASS** |
| `INV-MKT-04` | `buy_long` concurrency | Serializes parallel purchases (`2 × $700` on `$1000`) under `db._lock`: 1 fills, 1 fails with `FunMarketError`, cash remains `$300`. | [`test_economy_and_market_invariants.py`](../../tests/integration/test_economy_and_market_invariants.py) (`test_fun_market_parallel_buy_serializes_without_overdraft`) | **PASS** |

### C. Casino Game Math & Interactive UI Views ([`games/`](../../icbot/games/), [`features/`](../../icbot/features/))

| ID | Component | Invariant / Expected Contract | Test File & Node(s) | Status |
| :--- | :--- | :--- | :--- | :--- |
| `INV-GAME-01` | `blackjack_engine` | Exact multi-Ace total expansion, natural BJ (`int(1.5 * bet)`) vs split-21 (`1:1`), split (`max_splits`) & double (`2 * bet`) lifecycle, and immediate bust skipping dealer draw. | [`test_game_invariants.py`](../../tests/unit/test_game_invariants.py), [`test_game_engines.py`](../../tests/unit/test_game_engines.py) | **PASS** |
| `INV-GAME-02` | `Hand.is_soft` / `_dealer_should_hit` | Hard totals containing a forced-low Ace (e.g., `A+6+10` = `[17, 27]`, `A+6+10+4` = `[21, 31]`) must evaluate `is_soft() == False` so H17 dealers stand on hard 17. | [`test_game_invariants.py`](../../tests/unit/test_game_invariants.py) (`test_blackjack_hard_total_with_forced_low_ace_is_not_soft`) | **XFAIL** (`ISSUE-2B-11`) |
| `INV-GAME-03` | `roulette_engine` & `/ic casino roulette` | American 38-pocket partition (`18` red, `18` black, `0`, `-1` green), payouts across all 13 bet types, green `0`/`00` losing all 12 outside bets, end-to-end DB credit/debit, and ephemeral rejection of invalid bets/insufficient funds. | [`test_game_invariants.py`](../../tests/unit/test_game_invariants.py), [`test_casino_views_and_commands.py`](../../tests/integration/test_casino_views_and_commands.py) | **PASS** |
| `INV-GAME-04` | `slots_engine` & `/ic casino slots` / `payouts` | All 6 3-match multipliers (`50x, 20x, 10x, 5x, 3x, 2x`), `reel1 == reel2` `1x` push (characterizing `reel2 == reel3` loss vs payout text), DB balance settlement, and non-mutating `/ic casino payouts`. | [`test_game_invariants.py`](../../tests/unit/test_game_invariants.py), [`test_casino_views_and_commands.py`](../../tests/integration/test_casino_views_and_commands.py) | **PASS** |
| `INV-GAME-05` | `BlackjackView` lifecycle | Natural deal immediate settlement + `BlackjackTerminalView`, live `Hit -> Stand` flow, `Forfeit` penalty debit, `on_timeout()` auto-forfeit (with bot) & safe button disable (orphan view), post-settle double-click `"No active hand"`, and `interaction_check`. | [`test_casino_views_and_commands.py`](../../tests/integration/test_casino_views_and_commands.py) (`test_blackjack_natural_deal_win_bust_double_split_and_forfeit`) | **PASS** |
| `INV-GAME-06` | `blackjack._sessions` escrow | Active blackjack wagers survive bot process restart via upfront DB escrow or SQLite persistence instead of disappearing cleanly when `_sessions` resets. | [`test_casino_views_and_commands.py`](../../tests/integration/test_casino_views_and_commands.py) (`test_blackjack_unsettled_virtual_bet_survives_restart`) | **XFAIL** (`ISSUE-2B-05`) |
| `INV-GAME-07` | `BlackjackView.hit_btn` | Stopped/renewed views ignore stale queued button callbacks inside `_lock` (`self.is_finished()` guard). | [`test_casino_views_and_commands.py`](../../tests/integration/test_casino_views_and_commands.py) (`test_blackjack_stopped_view_rejects_duplicate_hit_callback`) | **XFAIL** (`ISSUE-2B-06`) |
| `INV-GAME-08` | `LeaderboardView` / `HelpPaginationView` | Enforces owner `interaction_check`, updates disabled prev/next state across page bounds, and disables all buttons on `on_timeout()`. | [`test_casino_views_and_commands.py`](../../tests/integration/test_casino_views_and_commands.py) (`test_leaderboard_and_help_pagination_views`) | **PASS** |

### D. Academic Features, RBAC & Embeds ([`exams.py`](../../icbot/features/exams.py), [`grades.py`](../../icbot/features/grades.py), [`notes.py`](../../icbot/features/notes.py), [`embeds.py`](../../icbot/embeds.py), [`permissions.py`](../../icbot/permissions.py))

| ID | Component | Invariant / Expected Contract | Test File & Node(s) | Status |
| :--- | :--- | :--- | :--- | :--- |
| `INV-ACAD-01` | `exams` & `permissions` | Requires `discord.Member` + admin/allowed role; `/ic exam add` rejects invalid dates, out-of-range hours, or `end_time < start_time` without DB writes; `/ic exam clear` enforces RBAC. | [`test_permissions_and_embeds.py`](../../tests/unit/test_permissions_and_embeds.py), [`test_casino_views_and_commands.py`](../../tests/integration/test_casino_views_and_commands.py) | **PASS** |
| `INV-ACAD-02` | `exams_add_cmd` -> `exams_remove_cmd` | Returned `ID` from `/ic exam add` identifies the inserted row for `/ic exam remove`. | [`test_casino_views_and_commands.py`](../../tests/integration/test_casino_views_and_commands.py) (`test_exams_add_returned_id_round_trips_into_remove`) | **XFAIL** (`ISSUE-2B-07`) |
| `INV-ACAD-03` | `grades_command` | Renders PNG distribution chart attachment on valid course HTML; 404/empty-prof error branches send error embed without raising `TypeError`. | [`test_casino_views_and_commands.py`](../../tests/integration/test_casino_views_and_commands.py) (`test_grades_command_success_sends_chart_attachment`, `test_grades_command_not_found_sends_error_embed_cleanly`) | **PASS** (200) / **XFAIL** (404, `ISSUE-2B-08`) |
| `INV-ACAD-04` | `notes` & `prof` | SPA notes index/bundle parser caches within 6h TTL; both scrapers return `[]`/`None` on HTTP/parse faults and propagate `CancelledError`. | [`test_discord_mock_commands.py`](../../tests/integration/test_discord_mock_commands.py), [`test_pollers_and_http_errors.py`](../../tests/integration/test_pollers_and_http_errors.py) | **PASS** |
| `INV-UI-01` | `FakeInteraction` / `embeds` | Single-response protocol (`defer`/`edit_message` marks done; double respond raises `InteractionResponded`; premature `followup` raises `RuntimeError`); alert/job builders stay within `256`/`1024`/`6000` bounds. | [`test_permissions_and_embeds.py`](../../tests/unit/test_permissions_and_embeds.py) | **PASS** |
| `INV-UI-02` | `embeds._base_embed` / `_add_fields` | Enforces Discord's hard API limits: `len(description) <= 4096` even with `subtitle`, `len(fields) <= 25`, and `len(embed) <= 6000`. | [`test_permissions_and_embeds.py`](../../tests/unit/test_permissions_and_embeds.py) (`test_embed_*_limit`) | **XFAIL** (`ISSUE-2B-09`) |

### E. Background Pollers, Checkpoints & Schema Migrations ([`tasks/`](../../icbot/tasks/), [`db.py`](../../icbot/db.py))

| ID | Component | Invariant / Expected Contract | Test File & Node(s) | Status |
| :--- | :--- | :--- | :--- | :--- |
| `INV-POLL-01` | `poll_and_post_jobs` | Suppresses historical burst when `jobs_bootstrap_post=False`, deduplicates via `job_seen`, filters senior titles & non-allowlisted locations, and skips 404 sources cached in `_jobs_bad_sources`. | [`test_pollers_and_http_errors.py`](../../tests/integration/test_pollers_and_http_errors.py) (`test_jobs_poller_checkpoint_dedupe_filtering_and_bad_source_cache`) | **PASS** |
| `INV-POLL-02` | `poll_and_post_workday_jobs` | Caches resolved CXS URL in `workday_cxs_cache` across 6 days within 7-day TTL and deduplicates postings via `job_seen`. | [`test_pollers_and_http_errors.py`](../../tests/integration/test_pollers_and_http_errors.py) (`test_workday_poller_cxs_cache_ttl_and_idempotency`) | **PASS** |
| `INV-POLL-03` | `poll_and_post_go_alerts` | Advances `go_last_servicealert_code` past trailing non-posted `INFO`/filtered messages, filters buses when `include_buses=False` and lines by allowlist, and re-baselines cleanly when checkpoint drops from feed retention. | [`test_pollers_and_http_errors.py`](../../tests/integration/test_pollers_and_http_errors.py) (`test_go_alerts_poller_checkpoint_advance_retention_reset_and_allowlist`) | **PASS** |
| `INV-POLL-04` | `poll_and_post_ttc` | Caches `ttc_account_id`, queries `since_id`, retries transient `TimeoutError`, aborts immediately on `429` (`TTCFetchError`) without retry loop, posts in chronological order advancing `ttc_last_status_id`, and propagates `CancelledError`. | [`test_pollers_and_http_errors.py`](../../tests/integration/test_pollers_and_http_errors.py) (`test_ttc_poller_idempotency_chronological_order_and_retry_vs_429`) | **PASS** |
| `INV-START-01` | `Database.init` / `FunMarketStore.sync_catalog` / `grades._init_db` | Repeated init & catalog sync preserve custom prices, user positions, and `meta`, mark delisted symbols `is_active=0` without deleting rows, and migrate legacy tables missing additive columns cleanly twice. | [`test_economy_and_market_invariants.py`](../../tests/integration/test_economy_and_market_invariants.py) (`test_startup_init_migrations_and_catalog_sync_idempotency`), [`test_pollers_and_http_errors.py`](../../tests/integration/test_pollers_and_http_errors.py) | **PASS** |

---

## 2. HTTP Provider Error-Family Behavior Matrix

Verified in [`tests/integration/test_pollers_and_http_errors.py`](../../tests/integration/test_pollers_and_http_errors.py) across all **12** external HTTP client modules (`AiohttpMockRouter`, offline-only):

| Client Module | `timeout` | `cancelled` | `429 Retry-After` | `5xx` | `malformed_json` / HTML | `empty_or_missing` |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| [`gemini_client.py`](../../icbot/services/gemini_client.py) | `TimeoutError` | `CancelledError` | `GeminiError(429)` | `GeminiError(503)` | `GeminiError` | `GeminiError` (blocked/empty) |
| [`go_client.py`](../../icbot/services/go_client.py) | `GOFetchError` | `CancelledError` | `GOFetchError` | `GOFetchError` | `GOFetchError` | `[]` |
| [`ttc_client.py`](../../icbot/services/ttc_client.py) | `TimeoutError` | `CancelledError` | `TTCFetchError` | `ClientResponseError` | `JSONDecodeError` | `[]` |
| [`jobs_ats_client.py`](../../icbot/services/jobs_ats_client.py) (GH & Lever) | `RuntimeError` | `CancelledError` | `RuntimeError` | `RuntimeError` | `RuntimeError` | `[]` |
| [`jobs_workday_client.py`](../../icbot/services/jobs_workday_client.py) | `TimeoutError` | `CancelledError` | `RuntimeError` | `RuntimeError` | `[]` (fallback empty) | `[]` |
| [`yahoo_quotes.py`](../../icbot/services/yahoo_quotes.py) | `TimeoutError` | `CancelledError` | `RuntimeError` | `RuntimeError` | `JSONDecodeError` | `{}` |
| [`btc_esplora.py`](../../icbot/services/btc_esplora.py) | `TimeoutError` | `CancelledError` | `RuntimeError` | `RuntimeError` | `JSONDecodeError` | `[]` |
| [`crypto_fng.py`](../../icbot/services/crypto_fng.py) | `TimeoutError` | `CancelledError` | `RuntimeError` | `RuntimeError` | `JSONDecodeError` | `None` |
| [`sports_client.py`](../../icbot/services/sports_client.py) | `TimeoutError` | `CancelledError` | `OddsAPIError` | `OddsAPIError` | `JSONDecodeError` | `[]` (plus TTL cache hit) |
| [`notes.py`](../../icbot/features/notes.py) | `[]` | `CancelledError` | `[]` | `[]` | `[]` | `[]` |
| [`prof.py`](../../icbot/features/prof.py) | `None` / `[]` | `CancelledError` | `None` / `[]` | `None` / `[]` | `None` / `[]` | `None` / `[]` |
| [`grades.py`](../../icbot/features/grades.py) | `TimeoutError` | `CancelledError` | `ClientResponseError` | `ClientResponseError` | `{"title": ..., "professors": []}` | `None` |

> **Cross-client observation:** `asyncio.CancelledError` propagates cleanly across all 12 modules. None of the HTTP clients inspect or sleep on the HTTP `Retry-After` response header (`ttc_alerts` stops immediately on `TTCFetchError`; `go_alerts` uses fixed `1.5 * (attempt + 1)` backoff).

---

## 3. Tracked Defect Register (`ISSUE-2B-01` .. `ISSUE-2B-11`)

Every defect below is reproduced with a strict `@pytest.mark.xfail(strict=True)` characterization test so remediation in Phase 3/4 immediately flips the case to `XPASS` and requires removing `xfail` to turn it into a permanent regression guard.

| ID | Source Location | Severity | Observed Buggy Behavior vs Expected Invariant | Test Node(s) | Planned Fix |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `ISSUE-2B-01` | [`user_service.py#L149-L171`](../../icbot/services/user_service.py#L149-L171) | **High** | Unlocked `SELECT`-before-`UPDATE` TOCTOU lets two concurrent `subtract_balance` calls read `$1000` and both settle at `$300` without `InsufficientBalanceError`. | `test_parallel_subtract_balance_prevents_overdraft_race` | **Phase 4** (atomic transaction / lock) |
| `ISSUE-2B-02` | [`user_service.py#L186-L213`](../../icbot/services/user_service.py#L186-L213) | **High** | Unlocked `SELECT`-before-`UPDATE` TOCTOU lets two concurrent `claim_daily` calls both pass the cooldown check and credit `$100` twice. | `test_parallel_claim_daily_awards_exactly_once` | **Phase 4** (atomic transaction / lock) |
| `ISSUE-2B-03` | [`user_service.py#L130-L171`](../../icbot/services/user_service.py#L130-L171), [`blackjack_engine.py#L147`](../../icbot/games/blackjack_engine.py#L147) | **High** | Missing `amount >= 1` / `bet >= 1` boundary check; `subtract_balance(uid, -500)` credits `+$500`, and `GameState.start(bet <= 0)` accepts `0`/`-100`. | `test_user_store_rejects_non_positive_amounts` `[0, -1, -500]`, `test_blackjack_engine_rejects_non_positive_bet` `[0, -1, -100]` | **Phase 4** |
| `ISSUE-2B-04` | [`user_service.py#L49-L68`](../../icbot/services/user_service.py#L49-L68) | **Medium** | Concurrent `ensure_user` calls on an unseen `user_id` race past `SELECT None` into duplicate `INSERT INTO users`, raising `sqlite3.IntegrityError`. | `test_parallel_ensure_user_registration_is_idempotent` | **Phase 4** (`INSERT OR IGNORE` / lock) |
| `ISSUE-2B-05` | [`blackjack.py#L156-L183`](../../icbot/features/blackjack.py#L156-L183) | **High** | Unsettled bets live only in RAM (`_sessions`) without SQLite escrow; restarting or clearing sessions discards active losing hands with `$0` deduction. | `test_blackjack_unsettled_virtual_bet_survives_restart` | **Phase 4** |
| `ISSUE-2B-06` | [`blackjack.py#L559-L585`](../../icbot/features/blackjack.py#L559-L585) | **Medium** | `BlackjackView.hit_btn` acquires `_lock` without checking `self.is_finished()`, letting duplicate callbacks on an already-renewed/stopped view mutate the active hand twice. | `test_blackjack_stopped_view_rejects_duplicate_hit_callback` | **Phase 4** |
| `ISSUE-2B-07` | [`exams.py#L222-L240`](../../icbot/features/exams.py#L222-L240) | **Medium** | `exams_add_cmd` queries `SELECT last_insert_rowid()` on a new pooled connection after `INSERT`, reporting `ID: 0` so `/ic exam add` -> `/ic exam remove` fails. | `test_exams_add_returned_id_round_trips_into_remove` | **Phase 4** |
| `ISSUE-2B-08` | [`grades.py#L191-L206`](../../icbot/features/grades.py#L191-L206), [`#L227-L232`](../../icbot/features/grades.py#L227-L232) | **Medium** | Passes unsupported `is_error=True` keyword argument to `embeds.system()`, raising `TypeError` on course 404, empty professor data, and outer exception handler. | `test_grades_command_not_found_sends_error_embed_cleanly` | **Phase 3 / 4** |
| `ISSUE-2B-09` | [`embeds.py#L58-L65`](../../icbot/embeds.py#L58-L65), [`#L92-L109`](../../icbot/embeds.py#L92-L109), [`#L155-L173`](../../icbot/embeds.py#L155-L173) | **Low / Medium** | `_base_embed` prepends `subtitle` after 4096-char truncation (exceeding 4096); `_add_fields` lacks a 25-field cap; builders do not trim combined length to 6000 chars. | `test_embed_description_with_subtitle_enforces_discord_4096_limit`, `test_embed_enforces_discord_25_field_limit`, `test_embed_enforces_discord_6000_total_char_limit` | **Phase 3 / 4** |
| `ISSUE-2B-10` | [`blackjack.py#L80-L114`](../../icbot/features/blackjack.py#L80-L114) | **High** | `BlackjackStore.apply_delta_and_stats` performs multiple unlocked `SELECT`+`UPDATE` queries, overwriting concurrent settlement deltas and `hands_played` increments. | `test_parallel_blackjack_apply_delta_preserves_all_updates` | **Phase 4** |
| `ISSUE-2B-11` | [`blackjack_engine.py#L92-L95`](../../icbot/games/blackjack_engine.py#L92-L95), [`#L231-L240`](../../icbot/games/blackjack_engine.py#L231-L240) | **Medium** | `Hand.is_soft()` checks `any(v <= 21 for v in vals)` independently of `any((v - 10) in vals for v in vals)`, misclassifying hard Ace totals (`A+6+10` = `[17, 27]`, `A+6+10+4` = `[21, 31]`) as soft so H17 dealers illegally hit hard 17. | `test_blackjack_hard_total_with_forced_low_ace_is_not_soft` `[[A,6,10], [A,6,10,4]]` | **Phase 4** |

---

## 4. Non-Test Code-Audited Findings & Deliberate Legacy Semantics

- **Gateway Reconnect Command Registration ([`main.py#L427-L433`](../../main.py#L427-L433)):** `on_ready()` calls `bot.tree.add_command(ic, guild=guild)` without `override=True` when `force_guild_resync=False`, which fails with `CommandAlreadyRegistered` on gateway reconnects that re-fire `on_ready`. (Identified by static inspection only per safety rule forbidding `main.py` execution; scheduled for Phase 3/4 runtime hardening.)
- **Slots Two-Match Rule Asymmetry ([`slots_engine.py#L59`](../../icbot/games/slots_engine.py#L59) vs [`#L91`](../../icbot/games/slots_engine.py#L91)):** `payout_table()` advertises `"Any two match — 1x (push)"`, while `spin()` awards `1x` only for `reel1 == reel2` (`reel2 == reel3` returns `match_type="none"`). Characterized as current engine behavior in `test_slots_two_match_push_and_loss_characterization`.
- **Rate-Limit `Retry-After` Headers:** HTTP `429` responses do not sleep for the advertised `Retry-After` interval in any client; [`ttc_alerts.py`](../../icbot/tasks/ttc_alerts.py) halts immediately on `TTCFetchError`, while [`go_alerts.py`](../../icbot/tasks/go_alerts.py) retries up to 3 times with fixed `1.5 * (attempt + 1)` backoff.
- **Notes Partial Multi-Site Caching ([`notes.py#L102-L117`](../../icbot/features/notes.py#L102-L117)):** Total multi-site failure returns `{}` without blocking immediate retry (`if cache_data` guard), whereas partial failure (1 site succeeds, 1 fails) caches the partial dictionary for the full 6-hour `_CACHE_TTL_SECONDS` window.

