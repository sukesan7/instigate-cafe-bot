"""
tools/smoke_offline.py — Zero-token, zero-network offline import and configuration smoke check.

Validates:
1. All tracked/source Python files parse cleanly via ast.parse().
2. Outbound INET socket connections and dotenv .env loading are blocked during module imports.
3. Importing ``main`` and ``icbot.bot`` produces zero side effects (no dotenv, no client/DB/tasks, no eager feature loads).
4. Settings.from_env() instantiates cleanly with empty secrets, disabled pollers, and gateway safeguard disabled.
5. Core game engines, market catalog, permissions, embeds, lore loader, and controlled /ic feature
   registry load and register all slash commands in memory without starting the Discord gateway.

Never invokes main.main() or bot.run().
"""

from __future__ import annotations

import ast
import asyncio
import os
import socket
import sys
from pathlib import Path
from typing import Any

SECRET_ENV_KEYS = (
    "DISCORD_TOKEN",
    "GEMINI_API_KEY",
    "GO_API_KEY",
    "ODDS_API_KEY",
    "ALPHAVANTAGE_API_KEY",
    "TTC_MASTODON_ACCT",
)

POLLER_OFF_OVERRIDES = {
    "LLM_ENABLED": "false",
    "MARKET_WATCH_ENABLED": "false",
    "MARKET_DAILY_REPORT_ENABLED": "false",
    "BTC_WATCH_ENABLED": "false",
    "JOBS_POLL_SECONDS": "0",
    "WORKDAY_POLL_SECONDS": "0",
    "JOBS_BOOTSTRAP_POST": "false",
    "TTC_BOOTSTRAP_POST_LATEST": "false",
    "ICBOT_ALLOW_GATEWAY": "false",
}


class OfflineSmokeNetworkError(RuntimeError):
    """Raised if any module attempts outbound networking during offline smoke checks."""


def _check_python_ast(repo_root: Path) -> int:
    roots = [
        repo_root / "main.py",
        repo_root / "icbot",
        repo_root / "tools",
        repo_root / "tests",
        repo_root / "scripts",
    ]
    py_files: list[Path] = []
    for target in roots:
        if target.is_file():
            py_files.append(target)
        elif target.is_dir():
            py_files.extend(
                p
                for p in sorted(target.rglob("*.py"))
                if "__pycache__" not in p.parts and ".venv" not in p.parts
            )

    for path in py_files:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return len(py_files)


def _install_offline_guards(repo_root: Path) -> dict[str, int]:
    for key in SECRET_ENV_KEYS:
        os.environ[key] = ""
    for key, val in POLLER_OFF_OVERRIDES.items():
        os.environ[key] = val

    # Isolate lore lookup to the public synthetic example pack
    os.environ["LORE_PATH"] = str(repo_root / "tools" / "lore.example.json")

    # Neutralize python-dotenv and track calls so import main never touches .env
    import dotenv

    dotenv_state = {"calls": 0}

    def _blocked_load_dotenv(*args: Any, **kwargs: Any) -> bool:
        dotenv_state["calls"] += 1
        return False

    dotenv.load_dotenv = _blocked_load_dotenv

    # Block IPv4/IPv6 connects on import (allow AF_UNIX for asyncio/aiosqlite internals)
    orig_connect = socket.socket.connect
    orig_connect_ex = socket.socket.connect_ex

    def _guarded_connect(self: socket.socket, address: Any) -> Any:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise OfflineSmokeNetworkError(f"Outbound connect blocked in smoke_offline: {address!r}")
        return orig_connect(self, address)

    def _guarded_connect_ex(self: socket.socket, address: Any) -> int:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise OfflineSmokeNetworkError(f"Outbound connect_ex blocked in smoke_offline: {address!r}")
        return orig_connect_ex(self, address)

    socket_cls: Any = socket.socket
    socket_cls.connect = _guarded_connect
    socket_cls.connect_ex = _guarded_connect_ex
    return dotenv_state


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    ast_count = _check_python_ast(repo_root)
    dotenv_state = _install_offline_guards(repo_root)

    # Verify importing main and icbot.bot triggers zero side effects
    import main as main_entry

    assert dotenv_state["calls"] == 0, "Expected import main not to invoke load_dotenv()"
    assert not hasattr(main_entry, "bot"), "Expected main module not to construct a bot on import"
    assert not [m for m in sys.modules if m.startswith("icbot.features.")], (
        "Expected import main not to eagerly import icbot.features.*"
    )

    from icbot.config import Settings

    settings = Settings.from_env()
    assert settings.token == "", "Expected empty DISCORD_TOKEN in offline smoke check"
    assert settings.allow_gateway is False, "Expected gateway safeguard disabled by default"
    assert settings.llm_enabled is False
    assert settings.market_watch_enabled is False
    assert settings.market_daily_report_enabled is False
    assert settings.btc_watch_enabled is False
    assert settings.jobs_poll_seconds == 0

    import icbot.app as app
    import icbot.embeds as embeds
    import icbot.games.blackjack_engine as blackjack_engine
    import icbot.games.roulette_engine as roulette_engine
    import icbot.games.slots_engine as slots_engine
    import icbot.permissions as permissions
    import icbot.registry as registry
    import icbot.services.fun_market_catalog as catalog
    import tools.lore_loader as lore_loader

    assert callable(embeds.system)
    assert callable(permissions.is_admin_or_allowed)
    assert len(catalog.FUN_STOCK_CATALOG) == 10
    bj_hand = blackjack_engine.Hand(
        cards=[
            blackjack_engine.Card("A", "♠"),
            blackjack_engine.Card("K", "♥"),
        ]
    )
    assert bj_hand.best_total() == 21
    assert len(roulette_engine.RED_NUMBERS | roulette_engine.BLACK_NUMBERS) == 36
    assert len(slots_engine.SYMBOLS) > 0

    lore = lore_loader.load_lore()
    assert len(lore) > 0, "Expected example lore entries to load"

    assert set(registry.FEATURE_MODULES) == set(registry.discover_feature_modules()), (
        "FEATURE_MODULES must match modules discovered under icbot.features"
    )
    bot = main_entry.create_bot(settings, register_commands=False, sync_commands=False)
    registry.register_ic_commands(bot.tree, override=True)
    asyncio.run(bot.close())

    commands_registered = list(app.ic.walk_commands())
    leaf_commands = registry.iter_leaf_commands()
    assert len(registry.FEATURE_MODULES) >= 17, "Expected >= 17 feature modules"
    assert len(leaf_commands) == 41, f"Expected 41 leaf commands, got {len(leaf_commands)}"

    print(
        f"[OK] Offline smoke check passed: "
        f"{ast_count} AST-checked files, "
        f"{len(registry.FEATURE_MODULES)} feature modules, "
        f"{len(commands_registered)} /ic nodes ({len(leaf_commands)} leaf commands), "
        f"{len(lore)} example lore entries loaded, "
        f"zero import side effects or gateway activity."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
