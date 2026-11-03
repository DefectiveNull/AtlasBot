"""Run the isolated community bot."""

import asyncio
import logging
import signal

from aibot.bot import Agent
from aibot.config import Config


async def main():
    config = Config.from_env()
    async with Agent(config) as bot:
        loop = asyncio.get_running_loop()
        for name in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(name, lambda: asyncio.create_task(bot.close()))
        await bot.start(config.discord_token)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())
