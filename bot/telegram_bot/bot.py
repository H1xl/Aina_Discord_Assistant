"""Фабрика Telegram-бота (aiogram 3)."""

import importlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from bot.core import texts
from bot.core.config import Config
from bot.db.repository import Repository
from bot.telegram_bot.handlers.help import router as help_router
from bot.telegram_bot.handlers.link import router as link_router

log = logging.getLogger(__name__)

# Команды ядра: (команда, описание) — для меню бота и /help.
CORE_COMMANDS = (
    ("help", texts.TG_CMD_HELP),
    ("start", texts.TG_CMD_START),
    ("servers", texts.TG_CMD_SERVERS),
    ("unlink", texts.TG_CMD_UNLINK),
)


def all_commands(discord_bot) -> list[tuple[str, str]]:
    """Команды ядра и всех загруженных модулей."""
    commands = list(CORE_COMMANDS)
    for module in discord_bot.modules:
        commands.extend(module.telegram_commands)
    return commands


def create_telegram_bot(config: Config) -> Bot:
    return Bot(token=config.telegram_token)


async def _set_commands(bot: Bot, discord_bot) -> None:
    try:
        await bot.set_my_commands([
            BotCommand(command=command, description=description)
            for command, description in all_commands(discord_bot)
        ])
    except Exception:
        log.exception("Не удалось задать меню команд Telegram")


def create_dispatcher(repo: Repository, discord_bot) -> Dispatcher:
    dp = Dispatcher()
    # workflow_data: aiogram внедрит их в хендлеры по имени аргумента.
    dp["repo"] = repo
    dp["discord_bot"] = discord_bot
    dp.startup.register(_set_commands)
    dp.include_router(help_router)
    dp.include_router(link_router)
    # Роутеры модулей, загруженных в процесс (включённость на конкретном
    # сервере каждый обработчик проверяет сам).
    for module in discord_bot.modules:
        for path in module.telegram_routers:
            dp.include_router(importlib.import_module(path).router)
    return dp
