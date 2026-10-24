"""Базовый класс когов модулей: учитывает, включён ли модуль на сервере."""

import discord
from discord.ext import commands

from bot.core import texts
from bot.discord_bot.helpers import respond


class ModuleCog(commands.Cog):
    """Ког модуля. Наследники задают ``module_name``.

    Slash-команды выключенного на сервере модуля отвечают вежливым отказом
    автоматически (interaction_check). Слушатели событий должны сами
    проверять ``await self.module_enabled(guild_id)`` в начале обработчика.
    """

    module_name: str = ""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = bot.config

    async def module_enabled(self, guild_id: int) -> bool:
        return await self.bot.is_module_enabled(guild_id, self.module_name)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or await self.module_enabled(interaction.guild.id):
            return True
        module = self.bot.get_module(self.module_name)
        await respond(
            interaction,
            texts.MODULE_DISABLED.format(title=module.title if module else self.module_name),
            colour=texts.EMBED_COLOR_WARNING,
        )
        return False
