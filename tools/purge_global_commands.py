import asyncio
import os
import aiohttp
import discord

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise SystemExit("DISCORD_TOKEN is missing in environment (.env not loaded?)")


async def purge():
    intents = discord.Intents.none()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            app = await client.application_info()
            app_id = app.id

            headers = {
                "Authorization": f"Bot {TOKEN}",
                "Content-Type": "application/json",
            }

            async with aiohttp.ClientSession(headers=headers) as session:
                url = f"https://discord.com/api/v10/applications/{app_id}/commands"
                async with session.put(url, json=[]) as resp:
                    body = await resp.text()
                    print(f"[PURGE] Global commands overwrite status={resp.status}")
                    if resp.status >= 300:
                        print(body)
                    else:
                        print("[PURGE] Global commands cleared successfully.")
        finally:
            await client.close()

    await client.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(purge())
