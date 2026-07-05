"""Точка входа: python -m bot.main"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from . import handlers
from .config import load_config
from .db import Database
from .llm import Parser

COMMANDS = [
    BotCommand(command="fridge", description="Что в холодильнике"),
    BotCommand(command="diary", description="Дневник еды с оценками"),
    BotCommand(command="today", description="Актуальный план готовки"),
    BotCommand(command="bind", description="Привязать этот групповой чат"),
    BotCommand(command="help", description="Как пользоваться"),
]


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config()

    handlers.ctx.config = config
    handlers.ctx.database = Database(config.db_path)
    handlers.ctx.parser = Parser(config.anthropic_api_key, config.llm_model)

    bot = Bot(token=config.bot_token)
    dp = Dispatcher()
    dp.include_router(handlers.router)

    await bot.set_my_commands(COMMANDS)
    logging.getLogger(__name__).info(
        "Старт polling. Пользователи: %s", ", ".join(config.users.values())
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
