# Instigate Cafe Bot — System Architecture & Execution Flows

**Version:** 2.0.0 (Modernized Service Architecture)  
**Storage Engine:** SQLite 3 in Write-Ahead Logging (WAL) mode  
**Framework:** Python 3.11+ / Discord.py 2.3+ (`app_commands`)  
**Design Principles:** Modular service container, strict dependency injection, single-instance scheduler ownership, and zero-token offline testability.

---

## 1. System Component Architecture

The modernized Instigate Cafe Bot is structured around a centralized **Service Container** and **Feature Registry** that decouple presentation (Discord UI interactions) from business logic, data persistence, and external HTTP providers.

```mermaid
flowchart TD
    subgraph DiscordPlatform["Discord Platform"]
        Gateway["Discord Gateway (WSS)"]
        Interactions["Application Commands (/ic)"]
        Channels["Text Channels / Announcements"]
    end

    subgraph CoreApplication["Bot Application Core"]
        Main["main.py (CLI Entrypoint & Safety Gates)"]
        Bot["CampusBot (discord.Client & Lifecycle Owner)"]
        Container["ServiceContainer (Dependency Injection)"]
        Registry["FeatureRegistry & Manifests (53 Commands)"]
        Supervisor["TaskSupervisor (8 Background Tasks)"]
    end

    subgraph DomainServices["Domain Engines & Services"]
        Economy["Economy & Casino Engines (Blackjack, Roulette, Slots)"]
        Academics["Academic Services (Notes, RMP, Exam Calendar)"]
        Careers["Jobs Engine & ATS Parsers (Greenhouse, Workday, Lever)"]
        LLMAssistant["Gemini Governance & Resume Engine"]
        Alerts["Deduplicated Alert Dispatcher"]
    end

    subgraph StorageLayer["Data & Persistence Layer"]
        Database["Database (aiosqlite Connection Pool)"]
        SQLiteDB[("campus.db (WAL Mode, Schema v7)")]
        AuditLedger[("Ledger & Transaction Tables")]
    end

    subgraph ExternalProviders["External Third-Party APIs"]
        GeminiAPI["Google Gemini API (REST)"]
        RMPAPI["RateMyProfessors (GraphQL)"]
        MetrolinxAPI["Metrolinx Open Data API (REST)"]
        MastodonAPI["TTC Mastodon Fediverse (REST)"]
        ATSBoards["Greenhouse / Lever / Workday (REST)"]
    end

    Gateway --> Bot
    Interactions --> Registry
    Registry --> Bot
    Bot --> Container
    Container --> Database
    Database --> SQLiteDB
    Database --> AuditLedger
    Bot --> Supervisor
    Supervisor --> Alerts
    Alerts --> Channels

    Container --> Economy
    Container --> Academics
    Container --> Careers
    Container --> LLMAssistant

    Academics -.-> RMPAPI
    Careers -.-> ATSBoards
    Alerts -.-> MetrolinxAPI
    Alerts -.-> MastodonAPI
    LLMAssistant -.-> GeminiAPI
```

---

## 2. Sequence Diagram 1: Slash Command Execution Flow

This diagram illustrates how an incoming slash command (e.g. `/ic notes`, `/ic profile`, `/ic exam list`) is routed, authenticated, guarded by module enablement rules, and resolved:

```mermaid
sequenceDiagram
    autonumber
    actor User as Discord Member
    participant Discord as Discord Gateway
    participant Tree as CommandTree (/ic)
    participant Gate as check_command_gate
    participant Feature as Feature Callback
    participant Container as ServiceContainer
    participant DB as SQLite (WAL)

    User->>Discord: Type /ic notes course:CPS109
    Discord->>Tree: Deliver Interaction Object
    Tree->>Tree: Check Cooldown & Role Permissions
    Tree->>Gate: check_command_gate(interaction, "academics")
    
    alt Feature Disabled for Guild
        Gate-->>Discord: Return Ephemeral "Feature disabled in this server"
        Discord-->>User: Display rejection notice
    else Feature Enabled
        Gate->>Feature: Invoke Command Callback
        Feature->>Discord: interaction.response.defer(thinking=True)
        Feature->>Container: Query AcademicRegistry / Cache
        Container->>DB: Check Server Manual Catalog (SELECT)
        DB-->>Container: Return catalog rows
        Container-->>Feature: Return Note URLs & Provenance
        Feature->>Discord: interaction.followup.send(embed=NotesEmbed, view=PaginationView)
        Discord-->>User: Render Rich Embed with Navigation
    end
```

---

## 3. Sequence Diagram 2: Periodic Background Poller Flow

This diagram illustrates how the `TaskSupervisor` schedules and executes background loops (e.g. transit alerts, job ingestion) with circuit breakers, exponential backoff, and deduplicated channel announcements:

```mermaid
sequenceDiagram
    autonumber
    participant Clock as Asyncio Loop / Clock
    participant Supervisor as TaskSupervisor
    participant Poller as Poller Task (e.g. poll_and_post_go)
    participant Circuit as Provider Circuit Breaker
    participant Provider as External Provider API
    participant Dispatcher as AlertDispatcher
    participant Discord as Discord Text Channel

    Clock->>Supervisor: Periodic Trigger (Interval Elapsed)
    Supervisor->>Supervisor: Check Guild Enablement & Settings
    Supervisor->>Poller: Execute task iteration
    Poller->>Circuit: Check circuit state (CLOSED/HALF-OPEN/OPEN)
    
    alt Circuit Open (Outage Active)
        Circuit-->>Poller: Fast-fail (Outage Dampened, No Request)
        Poller-->>Supervisor: Record skipped iteration
    else Circuit Closed (Healthy)
        Poller->>Provider: Outbound HTTPS Request (with timeout & jitter)
        alt Request Successful
            Provider-->>Poller: 200 OK (JSON Feed Payload)
            Circuit->>Circuit: Record Success (Reset failure counter)
            Poller->>Dispatcher: dispatch_alert(feed_id, payload)
            Dispatcher->>Dispatcher: Deduplicate against sliding LRU cache
            Dispatcher->>Discord: Send Channel Embed (if not duplicate)
            Poller-->>Supervisor: Record Success Health Snapshot
        else Request Failed / Timeout / 5xx
            Provider-->>Poller: Timeout or 503 Service Unavailable
            Circuit->>Circuit: Increment failure count / Open circuit
            Poller->>Poller: Log dampened warning via ProviderOutageTracker
            Poller-->>Supervisor: Record Failure Health Snapshot
        end
    end
```

---

## 4. Sequence Diagram 3: Atomic Economy & Market Transaction Flow

This diagram illustrates how casino games (Blackjack, Roulette, Slots) and virtual stock trades (`/ic icse buy/sell`) execute with double-entry balance verification, ACID invariants, and immediate WAL synchronization:

```mermaid
sequenceDiagram
    autonumber
    actor User as Discord Member
    participant Game as Casino / Market View
    participant Engine as Pure Engine (Math / Rules)
    participant UserStore as UserStore
    participant Ledger as SQLite WAL Transaction

    User->>Game: Click "Hit" or Bet 100 on Red
    Game->>Engine: Evaluate game state & payout multipliers
    Engine-->>Game: Hand Result / Settlement Amount (+100 or -100)

    Game->>UserStore: transfer_funds(user_id, guild_id, delta, reason)
    UserStore->>Ledger: BEGIN IMMEDIATE TRANSACTION
    
    Ledger->>Ledger: SELECT balance FROM users WHERE user_id=? FOR UPDATE
    
    alt Balance Insufficient (delta < 0 and balance + delta < 0)
        Ledger-->>UserStore: Raise InsufficientFundsError
        UserStore->>Ledger: ROLLBACK
        UserStore-->>Game: Reject Action
        Game-->>User: Display "Insufficient Balance" Ephemeral Warning
    else Balance Sufficient
        Ledger->>Ledger: UPDATE users SET balance = balance + delta WHERE user_id=?
        Ledger->>Ledger: INSERT INTO transactions (user_id, delta, reason, created_at) VALUES (...)
        Ledger->>Ledger: COMMIT (WAL Checkpoint Synchronized)
        Ledger-->>UserStore: Transaction Committed (New Balance)
        UserStore-->>Game: Settlement Verified
        Game-->>User: Edit Discord Message with Updated Hand & Payout
    end
```

---

## 5. Lifecycle Management and Clean Shutdown

The application guarantees clean process lifecycle handling through [`CampusBot._cleanup_runtime()`](file:///home/sukesan/dev/discord/campus-bot/icbot/bot.py):
1. **Signal Interception:** `SIGTERM` or `SIGINT` triggers `CampusBot.close()`.
2. **Supervisor Cancellation:** All active background asyncio tasks are cancelled with `supervisor.stop_all()`.
3. **HTTP Session Teardown:** Open `aiohttp.ClientSession` pools are drained and closed.
4. **Database Checkpointing:** SQLite WAL log is flushed and the connection is closed cleanly, preventing database corruption or stranded lock files.

