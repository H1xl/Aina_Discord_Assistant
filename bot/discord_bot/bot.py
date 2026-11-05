"""Фабрика Discord-бота: intents, коги ядра и модулей, синхронизация slash-команд."""

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.core import texts
from bot.core.config import Config
from bot.db.database import Database
from bot.db.repository import Repository
from bot.discord_bot.helpers import respond
from bot.modules import ModuleSpec, load_modules

log = logging.getLogger(__name__)

# Бот не подключается к голосовым каналам (заглушение снимается через REST API),
# поэтому PyNaCl/davey не нужны — гасим предупреждения об их отсутствии.
discord.VoiceClient.warn_nacl = False
discord.VoiceClient.warn_dave = False

# Коги ядра — загружаются всегда.
CORE_COGS = (
    "bot.discord_bot.cogs.telegram_link",
    "bot.discord_bot.cogs.modules",
    "bot.discord_bot.cogs.help",
)
# Ког приватного режима (/unlock) — только при PRIVATE_MODE.
PRIVACY_COG = "bot.discord_bot.cogs.privacy"
# Команды, доступные и на неразблокированном сервере.
OPEN_COMMANDS = frozenset({"unlock", "help"})


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
        # guild_id → {модуль: включён}; заполняется лениво из guild_modules.
        self._guild_modules: dict[int, dict[str, bool]] = {}
        # Приватный режим: разблокированные серверы. Загружаются из базы один
        # раз при старте; проверка перед командой — поиск в этом множестве.
        self.unlocked_guilds: set[int] = set()

    def get_module(self, name: str) -> Optional[ModuleSpec]:
        return next((m for m in self.modules if m.name == name), None)

    async def is_module_enabled(self, guild_id: int, name: str) -> bool:
        """Включён ли модуль на сервере (загружен в процесс и не выключен в /modules)."""
        module = self.get_module(name)
        if module is None:
            return False
        if guild_id not in self._guild_modules:
            self._guild_modules[guild_id] = await self.repo.get_guild_modules(guild_id)
        return self._guild_modules[guild_id].get(name, module.default_enabled)

    async def set_module_enabled(self, guild_id: int, name: str, enabled: bool) -> None:
        await self.repo.set_guild_module(guild_id, name, enabled)
        self._guild_modules.pop(guild_id, None)

    async def setup_hook(self) -> None:
        self.tree.on_error = self._on_app_command_error
        for cog in CORE_COGS:
            await self.load_extension(cog)
        for module in self.modules:
            await self.db.migrate(module.name, module.migrations)
            for extension in module.extensions:
                await self.load_extension(extension)
            log.info("Модуль «%s» (%s) загружен", module.title, module.name)

        if self.config.private_mode:
            # После миграций модулей: уже настроенные серверы видны по их таблицам.
            self.unlocked_guilds = await self.repo.load_unlocked_guilds()
            self.tree.interaction_check = self._private_mode_check
            await self.load_extension(PRIVACY_COG)
            log.info("Приватный режим: разблокировано серверов — %d", len(self.unlocked_guilds))

        if self.config.test_guild_id:
            guild = discord.Object(id=self.config.test_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Slash-команды синхронизированы с тестовым сервером %s",
                     self.config.test_guild_id)
        await self.tree.sync()
        log.info("Глобальная синхронизация slash-команд запрошена")

    async def _private_mode_check(self, interaction: discord.Interaction) -> bool:
        """Общая проверка перед каждой slash-командой в приватном режиме."""
        if interaction.guild_id is None or interaction.guild_id in self.unlocked_guilds:
            return True
        if (interaction.data or {}).get("name") in OPEN_COMMANDS:
            return True
        if interaction.type == discord.InteractionType.application_command:
            await respond(interaction, texts.PRIVATE_LOCKED, colour=texts.EMBED_COLOR_WARNING)
        return False

    async def _on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        # Отказ проверки (модуль выключен, нет прав) уже объяснён пользователю
        # в самой проверке — в лог его не пишем.
        if isinstance(error, app_commands.CheckFailure):
            return
        log.error("Ошибка slash-команды %s", interaction.command and interaction.command.name,
                  exc_info=error)

    async def on_ready(self) -> None:
        log.info("Discord-бот вошёл как %s (id=%s)", self.user, self.user.id)
