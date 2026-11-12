"""Explicit slash-command feature registry for Instigate Cafe Bot.

Replaces implicit side-effect imports in ``main.py`` with a deterministic,
testable registry while preserving the ``/ic`` command group hierarchy and
all leaf command qualified names.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Sequence
from types import ModuleType
from typing import Any

import discord
from discord import app_commands

import icbot.features as _features_pkg
from icbot.app import ic

# Explicit list of all feature modules that attach slash commands onto icbot.app.ic.
FEATURE_MODULES: tuple[str, ...] = (
    "icbot.features.admin",
    "icbot.features.blackjack",
    "icbot.features.exams",
    "icbot.features.grades",
    "icbot.features.help",
    "icbot.features.info",
    "icbot.features.invest",
    "icbot.features.jobs",
    "icbot.features.lc_leaderboard",
    "icbot.features.notes",
    "icbot.features.prof",
    "icbot.features.profile",
    "icbot.features.report",
    "icbot.features.resume",
    "icbot.features.roulette",
    "icbot.features.slots",
    "icbot.features.sports",
)


def discover_feature_modules() -> tuple[str, ...]:
    """Return sorted import paths for every non-private module under ``icbot.features``."""
    names = sorted(
        f"{_features_pkg.__name__}.{mod.name}"
        for mod in pkgutil.iter_modules(_features_pkg.__path__)
        if not mod.name.startswith("_")
    )
    return tuple(names)


def load_feature_modules(modules: Sequence[str] | None = None) -> tuple[ModuleType, ...]:
    """Import configured feature modules so their commands bind onto the ``/ic`` tree."""
    targets = tuple(modules) if modules is not None else FEATURE_MODULES
    return tuple(importlib.import_module(name) for name in targets)


def get_root_group() -> app_commands.Group:
    """Return the root ``/ic`` application command group."""
    return ic


def iter_leaf_commands(root: app_commands.Group | None = None) -> list[app_commands.Command[Any, Any, Any]]:
    """Return all leaf ``app_commands.Command`` objects registered under ``/ic``."""
    target = root or get_root_group()
    return [cmd for cmd in target.walk_commands() if isinstance(cmd, app_commands.Command)]


def registered_qualified_names(root: app_commands.Group | None = None) -> tuple[str, ...]:
    """Return sorted qualified names for every leaf command under ``/ic``."""
    return tuple(sorted(cmd.qualified_name for cmd in iter_leaf_commands(root)))


def register_ic_commands(
    tree: app_commands.CommandTree[Any],
    *,
    guild: discord.abc.Snowflake | None = None,
    modules: Sequence[str] | None = None,
    override: bool = True,
) -> app_commands.Group:
    """Load feature modules explicitly and bind ``/ic`` onto ``tree`` cleanly.

    Uses ``override=True`` so repeated registration remains idempotent without
    ever invoking ``tree.clear_commands()``.
    """
    load_feature_modules(modules)
    root = get_root_group()
    if guild is not None:
        tree.add_command(root, guild=guild, override=override)
    else:
        tree.add_command(root, override=override)
    return root

