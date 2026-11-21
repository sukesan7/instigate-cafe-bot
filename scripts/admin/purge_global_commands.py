"""Hardened destructive maintenance utility to purge Discord slash commands.

Relocated from ``tools/purge_global_commands.py`` to ``scripts/admin/``.

Safety invariants:
1. Zero import-time side effects: importing this module never reads environment
   tokens, never opens network connections, and never raises ``SystemExit``.
2. Explicit dry-run default: running without ``--execute`` prints the exact
   target scope, endpoint template, and required confirmation phrase without
   reading any bot token or performing any HTTP/Gateway I/O.
3. No implicit live token usage: live execution requires ``--token-env <VAR>``
   explicitly naming the environment variable holding the bot token.
4. Explicit confirmation & target echo: live execution verifies the bot
   application identity via Discord REST (no gateway connection required),
   echoes the target application/bot account and guild/global scope, and
   requires either interactive confirmation or ``--confirm --confirm-phrase``.
5. Safe redacted output: raw token values are scrubbed before printing errors.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

DISCORD_API_BASE = "https://discord.com/api/v10"


@dataclass(frozen=True)
class PurgePlan:
    """Resolved purge target and required confirmation phrase."""

    scope: str  # "global" or "guild"
    guild_id: int | None
    application_id: int | None
    execute: bool
    token_env: str | None
    confirm: bool
    confirm_phrase: str | None

    @property
    def expected_phrase(self) -> str:
        if self.scope == "guild" and self.guild_id is not None:
            return f"PURGE GUILD {self.guild_id}"
        return "PURGE GLOBAL COMMANDS"

    def endpoint_for(self, app_id: int | str) -> str:
        if self.scope == "guild" and self.guild_id is not None:
            return f"{DISCORD_API_BASE}/applications/{app_id}/guilds/{self.guild_id}/commands"
        return f"{DISCORD_API_BASE}/applications/{app_id}/commands"


def redact_token(text: str, token: str | None) -> str:
    """Ensure raw token text never appears in console output or error messages."""
    if not token:
        return text
    return text.replace(token, "[REDACTED]")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="purge_global_commands.py",
        description=(
            "Safely preview or purge Discord slash commands (defaults to --dry-run "
            "with zero network activity and zero implicit token access)."
        ),
    )
    parser.add_argument(
        "--scope",
        choices=("global", "guild"),
        default="global",
        help="Command registration scope to target (default: global).",
    )
    parser.add_argument(
        "--guild-id",
        type=int,
        default=None,
        help="Target Discord guild snowflake ID (required when --scope=guild).",
    )
    parser.add_argument(
        "--application-id",
        type=int,
        default=None,
        help="Optional application ID for dry-run URL preview.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Preview target scope and endpoint without contacting Discord (default).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Actually issue the destructive Discord REST PUT [] request.",
    )
    parser.add_argument(
        "--token-env",
        type=str,
        default=None,
        help=(
            "Name of the environment variable holding the bot token (required with --execute; "
            "never read implicitly)."
        ),
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        default=False,
        help="Non-interactive confirmation flag (must pair with matching --confirm-phrase).",
    )
    parser.add_argument(
        "--confirm-phrase",
        type=str,
        default=None,
        help='Exact confirmation phrase (e.g. "PURGE GLOBAL COMMANDS" or "PURGE GUILD <ID>").',
    )
    return parser


def parse_plan(argv: Sequence[str] | None = None) -> PurgePlan:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.scope == "guild" and (args.guild_id is None or args.guild_id <= 0):
        parser.error("--guild-id (positive snowflake integer) is required when --scope=guild.")
    if args.scope == "global" and args.guild_id is not None:
        parser.error("--guild-id cannot be combined with --scope=global; pass --scope=guild instead.")

    return PurgePlan(
        scope=args.scope,
        guild_id=args.guild_id,
        application_id=args.application_id,
        execute=bool(args.execute),
        token_env=args.token_env,
        confirm=bool(args.confirm),
        confirm_phrase=args.confirm_phrase,
    )


async def _default_fetch_identity(token: str) -> tuple[int, str]:
    """Resolve Discord application ID and bot account label via REST (no gateway)."""
    import aiohttp

    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=15)
    async with (
        aiohttp.ClientSession(headers=headers, timeout=timeout) as session,
        session.get(f"{DISCORD_API_BASE}/oauth2/applications/@me") as resp,
    ):
        if resp.status >= 300:
            body = redact_token(await resp.text(), token)
            raise RuntimeError(
                f"Failed to verify bot application identity (HTTP {resp.status}): {body[:200]}"
            )
        data: dict[str, Any] = await resp.json()
        app_id = int(data["id"])
        app_name = str(data.get("name") or "unknown-app")
        bot_info = data.get("bot") or {}
        bot_user = str(bot_info.get("username") or app_name)
        return app_id, f"{app_name} (bot={bot_user}, app_id={app_id})"


async def _default_put_empty_commands(token: str, endpoint: str) -> tuple[int, str]:
    """Overwrite the target Discord command scope with an empty list."""
    import aiohttp

    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=20)
    async with (
        aiohttp.ClientSession(headers=headers, timeout=timeout) as session,
        session.put(endpoint, json=[]) as resp,
    ):
        body = redact_token(await resp.text(), token)
        return resp.status, body


async def run_purge(
    plan: PurgePlan,
    *,
    env: dict[str, str] | None = None,
    fetch_identity: Callable[[str], Awaitable[tuple[int, str]]] | None = None,
    put_empty_commands: Callable[[str, str], Awaitable[tuple[int, str]]] | None = None,
    input_fn: Callable[[str], str] | None = None,
    out: Any = None,
    err: Any = None,
) -> int:
    """Execute dry-run preview or confirmed destructive purge."""
    stdout = out if out is not None else sys.stdout
    stderr = err if err is not None else sys.stderr
    env_map = env if env is not None else os.environ

    app_label: int | str = plan.application_id if plan.application_id is not None else "<APPLICATION_ID>"
    preview_url = plan.endpoint_for(app_label)
    target_desc = f"guild {plan.guild_id}" if plan.scope == "guild" else "GLOBAL (all servers)"

    if not plan.execute:
        print("[DRY-RUN] Destructive purge NOT executed (default safe mode).", file=stdout)
        print(f"  Target scope:    {target_desc}", file=stdout)
        print(f"  Target endpoint: PUT {preview_url}  payload=[]", file=stdout)
        print("  Token accessed:  None (pass --execute --token-env <VAR> to run live)", file=stdout)
        print(f'  Required phrase: "{plan.expected_phrase}"', file=stdout)
        return 0

    # Live execution safety gates
    if not plan.token_env:
        print(
            "[ERROR] Refusing live purge: --token-env <ENV_VAR_NAME> is required with --execute "
            "(this script never reads DISCORD_TOKEN implicitly).",
            file=stderr,
        )
        return 2

    token = (env_map.get(plan.token_env) or "").strip()
    if not token:
        print(
            f"[ERROR] Environment variable '{plan.token_env}' is empty or unset. Aborting safely.",
            file=stderr,
        )
        return 2

    identity_fn = fetch_identity or _default_fetch_identity
    put_fn = put_empty_commands or _default_put_empty_commands

    try:
        app_id, account_echo = await identity_fn(token)
    except (RuntimeError, OSError, TimeoutError, ValueError) as exc:
        print(f"[ERROR] {redact_token(str(exc), token)}", file=stderr)
        return 1

    endpoint = plan.endpoint_for(app_id)
    print("[TARGET CONFIRMATION]", file=stdout)
    print(f"  Account / App:   {account_echo}", file=stdout)
    print(f"  Target scope:    {target_desc}", file=stdout)
    print(f"  Target endpoint: PUT {endpoint}  payload=[]", file=stdout)

    if plan.confirm:
        if plan.confirm_phrase != plan.expected_phrase:
            print(
                f'[ERROR] Refusing purge: --confirm requires exact --confirm-phrase "{plan.expected_phrase}".',
                file=stderr,
            )
            return 2
    else:
        prompt_reader = input_fn or input
        if input_fn is None and not sys.stdin.isatty():
            print(
                f"[ERROR] Non-interactive shell detected. Pass --confirm --confirm-phrase "
                f'"{plan.expected_phrase}" to execute.',
                file=stderr,
            )
            return 2
        typed = prompt_reader(f'Type "{plan.expected_phrase}" to confirm destructive purge: ').strip()
        if typed != plan.expected_phrase:
            print("[ABORTED] Confirmation phrase did not match. No commands were modified.", file=stderr)
            return 2

    try:
        status, body = await put_fn(token, endpoint)
    except (RuntimeError, OSError, TimeoutError, ValueError) as exc:
        print(f"[ERROR] Purge request failed: {redact_token(str(exc), token)}", file=stderr)
        return 1

    print(f"[PURGE] HTTP status={status} target={target_desc}", file=stdout)
    if status >= 300:
        print(f"[ERROR] Discord API rejected purge: {redact_token(body, token)[:300]}", file=stderr)
        return 1

    print(f"[OK] Successfully cleared commands for {target_desc} ({account_echo}).", file=stdout)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    plan = parse_plan(argv)
    return asyncio.run(run_purge(plan))


if __name__ == "__main__":
    sys.exit(main())
