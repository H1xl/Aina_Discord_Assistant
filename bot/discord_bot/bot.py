"""Фабрика Discord-бота: intents, коги ядра и модулей, синхронизация slash-команд."""

import logging

import discord
from discord.ext import commands

from bot.core import texts
from bot.core.config import Config
from bot.db.database import Database
from bot.db.repository import Repository
from bot.modules import load_modules

log = logging.getLogger(__name__)

# Бот не подключается к голосовым каналам (заглушение снимается через REST API),
# поэтому PyNaCl/davey не нужны — гасим предупреждения об их отсутствии.
discord.VoiceClient.warn_nacl = False
discord.VoiceClient.warn_dave = False

# Коги ядра — загружаются всегда.
CORE_COGS = (
    "bot.discord_bot.cogs.telegram_link",
)


class AinaBot(commands.Bot):
    def __init__(self, config: Config, db: Database, repo: Repository):
        intents = discord.Intents.default()
        intents.members = True  # обязателен для on_member_join/on_member_remove
        # voice_states (защита от мута) входит в Intents.default().
        # message_content не нужен: бот работает только на slash-командах.
        # Префикс «по упоминанию» — заглушка: текстовых команд нет, и с ним
        # discord.py не предупреждает об отсутствии message_content.
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            activity=discord.Activity(
                type=discord.ActivityType.listening, name=texts.BOT_ACTIVITY
            ),
        )
        self.config = config
        self.db = db
        self.repo = repo  # репозиторий ядра; модули создают свои поверх self.db
        self.tg_bot = None  # aiogram.Bot, проставляется в main.py
        self.modules = load_modules(config.enabled_modules)

    async def setup_hook(self) -> None:
        for cog in CORE_COGS:
            await self.load_extension(cog)
        for module in self.modules:
            await self.db.migrate(module.name, module.migrations)
            for extension in module.extensions:
                await self.load_extension(extension)
            log.info("Модуль «%s» (%s) загружен", module.title, module.name)

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
