"""CLI entrypoint for Instigate Cafe Bot.

Importing this module performs no runtime I/O, dotenv loading, client creation,
feature registration, or network activity. Launching the live Discord gateway
requires explicit operator opt-in via ``ICBOT_ALLOW_GATEWAY=true`` or
``--allow-gateway``.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence

import dotenv

from icbot.bot import CampusBot, create_bot, validate_gateway_launch
from icbot.config import Settings
from icbot.errors import ConfigValidationError, redact_secrets

__all__ = ["CampusBot", "build_parser", "create_bot", "main", "validate_gateway_launch"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Instigate Cafe Discord Bot.")
    parser.add_argument(
        "--allow-gateway",
        action="store_true",
        help="Explicit opt-in required to connect to the Discord gateway.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    dotenv.load_dotenv(override=False)
    try:
        settings = Settings.load_validated()
    except ConfigValidationError as exc:
        raw_secrets = [
            os.getenv("DISCORD_TOKEN", ""),
            os.getenv("GEMINI_API_KEY", ""),
            os.getenv("GO_API_KEY", ""),
            os.getenv("ODDS_API_KEY", ""),
            os.getenv("ALPHAVANTAGE_API_KEY", ""),
        ]
        raise SystemExit(f"Configuration error: {redact_secrets(str(exc), raw_secrets)}") from exc

    validate_gateway_launch(settings, allow_gateway=bool(args.allow_gateway))
    if not settings.token:
        raise SystemExit("Missing DISCORD_TOKEN in environment or .env")

    bot = create_bot(settings)
    bot.run(settings.token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
