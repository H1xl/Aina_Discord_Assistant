"""Фабрика Telegram-бота (aiogram 3)."""

import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from bot.core import texts
from bot.core.config import Config
from bot.db.repository import Repository
from bot.telegram_bot.handlers.link import router as link_router

log = logging.getLogger(__name__)


def create_telegram_bot(config: Config) -> Bot:
    return Bot(token=config.telegram_token)


async def _set_commands(bot: Bot) -> None:
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description=texts.TG_CMD_START),
            BotCommand(command="servers", description=texts.TG_CMD_SERVERS),
            BotCommand(command="unlink", description=texts.TG_CMD_UNLINK),
        ])
    except Exception:
        log.exception("Не удалось задать меню команд Telegram")


def create_dispatcher(repo: Repository, discord_bot) -> Dispatcher:
    dp = Dispatcher()
    # workflow_data: aiogram внедрит их в хендлеры по имени аргумента.
    dp["repo"] = repo
    dp["discord_bot"] = discord_bot
    dp.startup.register(_set_commands)
    dp.include_router(link_router)
    return dp
