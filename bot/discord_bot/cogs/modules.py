"""/modules — включение и выключение модулей на конкретном сервере.

Ког ядра. Модуль, загруженный в процесс (ENABLED_MODULES), работает на
сервере, если здесь не выключен; новые серверы получают состояние
по умолчанию из ModuleSpec.default_enabled.
"""

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.core import texts
from bot.discord_bot.helpers import deny_admin, expire_view_message, is_admin


class ModulesView(discord.ui.View):
    def __init__(self, cog: "ModulesCog", author_id: int, enabled: set[str]):
        super().__init__(timeout=600)
        self.cog = cog
        self.author_id = author_id
        self.enabled = set(enabled)
        self.message: Optional[discord.Message] = None
        self._finished = False

        modules = cog.bot.modules
        self.select = discord.ui.Select(
            placeholder=texts.MODULES_SELECT,
            min_values=0,
            max_values=len(modules),
            options=[
                discord.SelectOption(
                    label=module.title[:100],
                    description=(module.description or None) and module.description[:100],
                    value=module.name,
                    default=module.name in self.enabled,
                )
                for module in modules
            ],
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

        save = discord.ui.Button(label=texts.MODULES_SAVE_BUTTON, style=discord.ButtonStyle.success)
        save.callback = self._on_save
        self.add_item(save)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    async def on_timeout(self) -> None:
        if not self._finished:
            await expire_view_message(self.message)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        self.enabled = set(self.select.values)
        await interaction.response.defer()

    async def _on_save(self, interaction: discord.Interaction) -> None:
        self._finished = True
        self.stop()
        await self.cog.save(interaction, self.enabled)


class ModulesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _render(self, enabled: set[str]) -> str:
        return "\n".join(
            texts.MODULES_LINE.format(
                mark="✅" if module.name in enabled else "▫️",
                title=module.title,
                description=module.description,
            )
            for module in self.bot.modules
        )

    @app_commands.command(name="modules", description="Включить или выключить модули бота на сервере (админ)")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def modules_command(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction):
            await deny_admin(interaction)
            return
        guild_id = interaction.guild.id
        enabled = {
            module.name for module in self.bot.modules
            if await self.bot.is_module_enabled(guild_id, module.name)
        }
        view = ModulesView(self, interaction.user.id, enabled)
        embed = discord.Embed(
            title=texts.MODULES_TITLE,
            description=texts.MODULES_PROMPT.format(modules=self._render(enabled)),
            colour=texts.EMBED_COLOR,
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

    async def save(self, interaction: discord.Interaction, enabled: set[str]) -> None:
        guild_id = interaction.guild.id
        for module in self.bot.modules:
            await self.bot.set_module_enabled(guild_id, module.name, module.name in enabled)
        await self.bot.repo.add_action_log(
            guild_id, "modules", f"enabled={','.join(sorted(enabled))} by={interaction.user.id}"
        )
        embed = discord.Embed(
            title=texts.MODULES_TITLE,
            description=texts.MODULES_SAVED.format(modules=self._render(enabled)),
            colour=texts.EMBED_COLOR_SUCCESS,
        )
        await interaction.response.edit_message(embed=embed, view=None)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ModulesCog(bot))
