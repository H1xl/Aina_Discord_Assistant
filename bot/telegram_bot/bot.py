"""Фабрика Telegram-бота (aiogram 3)."""

from aiogram import Bot, Dispatcher

from bot.core.config import Config
from bot.db.repository import Repository
from bot.telegram_bot.handlers.link import router as link_router


def create_telegram_bot(config: Config) -> Bot:
    return Bot(token=config.telegram_token)


def create_dispatcher(repo: Repository, discord_bot) -> Dispatcher:
    dp = Dispatcher()
    # workflow_data: aiogram внедрит их в хендлеры по имени аргумента.
    dp["repo"] = repo
    dp["discord_bot"] = discord_bot
    dp.include_router(link_router)
    return dp
