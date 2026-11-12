"""Unit tests for import side effects, command registry, and gateway launch safeguard."""

from __future__ import annotations

import asyncio
import importlib
import os
import subprocess
import sys
from pathlib import Path

import discord
import pytest
from discord import app_commands

pytestmark = pytest.mark.unit

EXPECTED_LEAF_COMMAND_NAMES = {
    "ic botinfo",
    "ic dailyclaim",
    "ic grades",
    "ic help",
    "ic jobs",
    "ic leaderboard",
    "ic notes",
    "ic prof",
    "ic profile",
    "ic report",
    "ic resume",
    "ic serverinfo",
    "ic admin llm_start",
    "ic admin llm_stop",
    "ic admin move",
    "ic admin set",
    "ic admin sync_catalog",
    "ic admin weekroll",
    "ic casino blackjack",
    "ic casino payouts",
    "ic casino roulette",
    "ic casino slots",
    "ic exam add",
    "ic exam clear",
    "ic exam list",
    "ic exam remove",
    "ic icse buy",
    "ic icse cover",
    "ic icse history",
    "ic icse market",
    "ic icse portfolio",
    "ic icse quote",
    "ic icse sell",
    "ic icse short",
    "ic icse stock",
    "ic icse trades",
    "ic sports boxing",
    "ic sports mma",
    "ic sports nba",
    "ic sports nfl",
    "ic sports ufc",
}


def test_importing_main_and_bot_has_zero_side_effects(monkeypatch: pytest.MonkeyPatch):
    """Importing main/icbot.bot in a fresh interpreter must not load .env, features, or clients."""
    import dotenv

    repo_root = Path(__file__).resolve().parents[2]
    probe = (
        "import sys, dotenv\n"
        "calls = 0\n"
        "def _spy(*a, **kw):\n"
        "    global calls\n"
        "    calls += 1\n"
        "    return False\n"
        "dotenv.load_dotenv = _spy\n"
        "import main, icbot.bot, icbot.registry\n"
        "assert calls == 0, f'load_dotenv called {calls} times on import'\n"
        "assert not hasattr(main, 'bot')\n"
        "assert not hasattr(main, 'db')\n"
        "assert not hasattr(main, 'settings')\n"
        "assert not [m for m in sys.modules if m.startswith('icbot.features.')]\n"
        "assert callable(main.main) and callable(icbot.bot.create_bot)\n"
    )
    safe_env = {
        k: v
        for k, v in os.environ.items()
        if k in ("PATH", "HOME", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR", "TMPDIR")
    }
    safe_env["DISCORD_TOKEN"] = ""
    safe_env["ICBOT_ALLOW_GATEWAY"] = "false"
    res = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=repo_root,
        env=safe_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"stderr: {res.stderr}\nstdout: {res.stdout}"

    # Also verify in-process module attributes cleanly without mutating loaded icbot.features
    dotenv_calls = 0

    def _forbidden_load_dotenv(*args, **kwargs):
        nonlocal dotenv_calls
        dotenv_calls += 1
        return False

    monkeypatch.setattr(dotenv, "load_dotenv", _forbidden_load_dotenv)
    main_mod = importlib.reload(importlib.import_module("main"))
    bot_mod = importlib.reload(importlib.import_module("icbot.bot"))
    assert dotenv_calls == 0
    assert callable(main_mod.main)
    assert callable(bot_mod.create_bot)


def test_registry_covers_all_feature_modules_and_preserves_ic_routes():
    from icbot import registry

    discovered = set(registry.discover_feature_modules())
    configured = set(registry.FEATURE_MODULES)
    assert configured == discovered
    assert len(registry.FEATURE_MODULES) == 17

    registry.load_feature_modules()
    names = set(registry.registered_qualified_names())
    assert names == EXPECTED_LEAF_COMMAND_NAMES
    assert len(list(registry.get_root_group().walk_commands())) == 46


async def test_register_ic_commands_is_idempotent_with_override():
    from icbot import registry

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    tree: app_commands.CommandTree[discord.Client] = app_commands.CommandTree(client)
    guild = discord.Object(id=900000000000000001)

    registry.register_ic_commands(tree, guild=guild, override=True)
    registry.register_ic_commands(tree, guild=guild, override=True)

    guild_cmds = tree.get_commands(guild=guild)
    assert [c.name for c in guild_cmds] == ["ic"]
    await client.close()


def test_gateway_safeguard_blocks_launch_without_explicit_opt_in(monkeypatch: pytest.MonkeyPatch, tmp_path):
    import dotenv

    import main as main_mod
    from icbot.bot import CampusBot

    load_calls = 0

    def _spy_dotenv(*args, **kwargs):
        nonlocal load_calls
        load_calls += 1
        assert kwargs.get("override") is False
        return False

    monkeypatch.setattr(dotenv, "load_dotenv", _spy_dotenv)
    monkeypatch.setenv("DISCORD_TOKEN", "synthetic-token-value")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "safeguard.db"))
    monkeypatch.setenv("ICBOT_ALLOW_GATEWAY", "false")

    run_calls: list[str] = []
    monkeypatch.setattr(CampusBot, "run", lambda self, token, **kw: run_calls.append(token))

    with pytest.raises(SystemExit) as exc_info:
        main_mod.main([])

    assert load_calls == 1
    assert "ICBOT_ALLOW_GATEWAY=true" in str(exc_info.value)
    assert "--allow-gateway" in str(exc_info.value)
    assert run_calls == []


@pytest.mark.parametrize(
    ("env_val", "argv"),
    [
        ("true", []),
        ("false", ["--allow-gateway"]),
    ],
)
def test_gateway_safeguard_allows_explicit_opt_in_with_mocked_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path, env_val: str, argv: list[str]
):
    import main as main_mod
    from icbot.bot import CampusBot

    monkeypatch.setenv("DISCORD_TOKEN", "synthetic-token-value")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "optin.db"))
    monkeypatch.setenv("ICBOT_ALLOW_GATEWAY", env_val)

    captured: list[tuple[CampusBot, str]] = []

    def _fake_run(self: CampusBot, token: str, **kwargs):
        captured.append((self, token))

    monkeypatch.setattr(CampusBot, "run", _fake_run)

    rc = main_mod.main(argv)
    assert rc == 0
    assert len(captured) == 1
    bot, token = captured[0]
    assert token == "synthetic-token-value"
    assert bot.setup_complete is False
    asyncio.run(bot.close())
