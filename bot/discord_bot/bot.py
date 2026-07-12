"""Фабрика Discord-бота: intents, коги, синхронизация slash-команд."""

import logging

import discord
from discord.ext import commands

from bot.core import texts
from bot.core.config import Config
from bot.db.repository import Repository

log = logging.getLogger(__name__)

COGS = (
    "bot.discord_bot.cogs.verification",
    "bot.discord_bot.cogs.setup",
    "bot.discord_bot.cogs.connect",
)


class AinaBot(commands.Bot):
    def __init__(self, config: Config, repo: Repository):
        intents = discord.Intents.default()
        intents.members = True  # обязателен для on_member_join/on_member_remove
        # message_content не нужен: бот работает только на slash-командах.
        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
            activity=discord.Activity(
                type=discord.ActivityType.listening, name=texts.BOT_ACTIVITY
            ),
        )
        self.config = config
        self.repo = repo
        self.tg_bot = None  # aiogram.Bot, проставляется в main.py

    async def setup_hook(self) -> None:
        for cog in COGS:
            await self.load_extension(cog)

        if self.config.test_guild_id:
            guild = discord.Object(id=self.config.test_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Slash-команды синхронизированы с тестовым сервером %s",
                     self.config.test_guild_id)
        await self.tree.sync()
        log.info("Глобальная синхронизация slash-команд запрошена")

    async def on_ready(self) -> None:
        log.info("Discord-бот вошёл как %s (id=%s)", self.user, self.user.id)
