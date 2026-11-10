"""Manual live diagnostic for Yahoo Finance cookie/crumb quote fetching.

Not collected by pytest. Run manually when verifying live Yahoo Finance responses:
    python3 scripts/manual/test_yf.py
"""

from __future__ import annotations

import asyncio

import aiohttp

from icbot.services.yahoo_quotes import fetch_quotes


async def main() -> None:
    try:
        quotes = await fetch_quotes(["SPY", "QQQ"], debug=True)
        for sym, q in quotes.items():
            print(f"--- {sym} ---")
            print(f"market_state: {q.market_state}")
            print(f"regular_price: {q.regular_price}, reg_pct: {q.regular_change_pct}")
            print(f"pre_price: {q.pre_price}, pre_pct: {q.pre_change_pct}")
            print(f"post_price: {q.post_price}, post_pct: {q.post_change_pct}")
            print(f"best_session: {q.best_session()}")
    except (aiohttp.ClientError, TimeoutError, RuntimeError, ValueError, KeyError, OSError) as exc:
        print(f"Error fetching quotes: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
