"""
tools/smoke_offline.py — Zero-token, zero-network offline import and configuration smoke check.

Validates:
1. All tracked/source Python files parse cleanly via ast.parse().
2. Outbound INET socket connections and dotenv .env loading are blocked during module imports.
3. Settings.from_env() instantiates cleanly with empty secrets and disabled background pollers.
4. Core game engines, market catalog, permissions, embeds, lore loader, and all /ic feature
   modules import and register slash commands in memory without starting the Discord gateway.

Never invokes main.main() or bot.run().
"""

from __future__ import annotations

import ast
import importlib
import os
import pkgutil
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


def _install_offline_guards(repo_root: Path) -> None:
    for key in SECRET_ENV_KEYS:
        os.environ[key] = ""
    for key, val in POLLER_OFF_OVERRIDES.items():
        os.environ[key] = val

    # Isolate lore lookup to the public synthetic example pack
    os.environ["LORE_PATH"] = str(repo_root / "tools" / "lore.example.json")

    # Neutralize python-dotenv so local workspace .env is never read during offline checks
    import dotenv

    dotenv.load_dotenv = lambda *args, **kwargs: False

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


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    ast_count = _check_python_ast(repo_root)
    _install_offline_guards(repo_root)

    from icbot.config import Settings

    settings = Settings.from_env()
    assert settings.token == "", "Expected empty DISCORD_TOKEN in offline smoke check"
    assert settings.llm_enabled is False
    assert settings.market_watch_enabled is False
    assert settings.market_daily_report_enabled is False
    assert settings.btc_watch_enabled is False
    assert settings.jobs_poll_seconds == 0

    import icbot.app as app
    import icbot.embeds as embeds
    import icbot.features as features_pkg
    import icbot.games.blackjack_engine as blackjack_engine
    import icbot.games.roulette_engine as roulette_engine
    import icbot.games.slots_engine as slots_engine
    import icbot.permissions as permissions
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

    imported_features = 0
    for mod in sorted(pkgutil.iter_modules(features_pkg.__path__), key=lambda m: m.name):
        importlib.import_module(f"icbot.features.{mod.name}")
        imported_features += 1

    commands_registered = list(app.ic.walk_commands())
    assert imported_features >= 17, f"Expected >= 17 feature modules, got {imported_features}"
    assert len(commands_registered) >= 17, f"Expected registered commands, got {len(commands_registered)}"

    print(
        f"[OK] Offline smoke check passed: "
        f"{ast_count} AST-checked files, "
        f"{imported_features} feature modules, "
        f"{len(commands_registered)} /ic commands registered, "
        f"{len(lore)} example lore entries loaded, "
        f"zero network or gateway activity."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
