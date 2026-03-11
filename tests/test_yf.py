import asyncio
from icbot.services.yahoo_quotes import fetch_quotes

async def main():
    try:
        quotes = await fetch_quotes(["SPY", "QQQ"], debug=True)
        for sym, q in quotes.items():
            print(f"--- {sym} ---")
            print(f"market_state: {q.market_state}")
            print(f"regular_price: {q.regular_price}, reg_pct: {q.regular_change_pct}")
            print(f"pre_price: {q.pre_price}, pre_pct: {q.pre_change_pct}")
            print(f"post_price: {q.post_price}, post_pct: {q.post_change_pct}")
            print(f"best_session: {q.best_session()}")
    except Exception as e:
        print(f"Error fetching quotes: {e}")

if __name__ == "__main__":
    asyncio.run(main())
