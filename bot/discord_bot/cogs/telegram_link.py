"""Привязка Telegram-чата к серверу: /connect.

Ког ядра — загружается всегда, независимо от включённых модулей.
"""

import secrets

import discord
from discord import app_commands
from discord.ext import commands

from bot.core import texts
from bot.discord_bot.helpers import deny_admin, is_admin

# Без похожих символов (0/O, 1/I), чтобы код было легко перепечатать.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6


def generate_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


class TelegramLinkCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.repo = bot.repo
        self.config = bot.config

    @app_commands.command(
        name="connect",
        description="Получить код для привязки Telegram-чата (админ)",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def connect_command(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction):
            await deny_admin(interaction)
            return

        await self.repo.cleanup_expired_codes()
        code = generate_code()
        await self.repo.create_connect_code(
            guild_id=interaction.guild.id,
            code=code,
            created_by=interaction.user.id,
            ttl_seconds=self.config.connect_code_ttl,
        )
        await self.repo.add_action_log(
            interaction.guild.id, "connect_code", f"by={interaction.user.id}"
        )

        embed = discord.Embed(
            title=texts.CONNECT_TITLE,
            description=texts.CONNECT_CODE_TEXT.format(
                code=code, minutes=self.config.connect_code_ttl // 60
            ),
            colour=texts.EMBED_COLOR,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TelegramLinkCog(bot))
