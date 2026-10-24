"""/help в Discord: кто я, команды по разделам и модули сервера.

Справка собирается из зарегистрированных slash-команд: команды когов
модулей группируются по модулю (с отметкой, включён ли он на сервере),
остальные — в раздел «Основное». Новые модули попадают сюда сами.
"""

import discord
from discord import app_commands
from discord.ext import commands

from bot.core import texts


def _is_admin_command(command: app_commands.Command) -> bool:
    perms = command.default_permissions
    return perms is not None and perms.administrator


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _command_lines(self, cog: commands.Cog) -> list[str]:
        return [
            texts.HELP_COMMAND_LINE.format(
                name=command.name,
                # «(админ)» в описании команды заменяется значком 🔒.
                description=command.description.removesuffix(" (админ)"),
                admin=texts.HELP_ADMIN_MARK if _is_admin_command(command) else "",
            )
            for command in cog.get_app_commands()
            if isinstance(command, app_commands.Command)
        ]

    @app_commands.command(name="help", description="Что я умею: команды и модули")
    async def help_command(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        core_lines: list[str] = []
        module_lines: dict[str, list[str]] = {}
        for cog in self.bot.cogs.values():
            name = getattr(cog, "module_name", "")
            if name:
                module_lines.setdefault(name, []).extend(self._command_lines(cog))
            else:
                core_lines.extend(self._command_lines(cog))

        embed = discord.Embed(
            title=texts.HELP_TITLE, description=texts.HELP_INTRO, colour=texts.EMBED_COLOR
        )
        embed.add_field(name=texts.HELP_CORE_SECTION, value="\n".join(core_lines)[:1024], inline=False)
        for module in self.bot.modules:
            enabled = guild is None or await self.bot.is_module_enabled(guild.id, module.name)
            lines = [f"*{module.description}*"] + module_lines.get(module.name, [])
            if not module_lines.get(module.name):
                lines.append(texts.HELP_TELEGRAM_ONLY)
            embed.add_field(
                name=(texts.HELP_MODULE_ON if enabled else texts.HELP_MODULE_OFF).format(
                    title=module.title
                ),
                value="\n".join(lines)[:1024],
                inline=False,
            )
        embed.set_footer(text=texts.HELP_FOOTER)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HelpCog(bot))
