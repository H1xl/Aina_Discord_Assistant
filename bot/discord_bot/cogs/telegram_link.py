"""Привязка Telegram-чатов к серверу: /connect и /telegram-chats.

Ког ядра — загружается всегда, независимо от включённых модулей.
"""

import secrets
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.core import texts
from bot.db.models import CODE_KIND_ADMIN
from bot.discord_bot.helpers import deny_admin, expire_view_message, is_admin, respond

# Без похожих символов (0/O, 1/I), чтобы код было легко перепечатать.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6


def generate_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def _chat_label(row) -> str:
    return f"{row['chat_title'] or texts.TG_CHAT_UNTITLED} (ID {row['telegram_chat_id']})"


class UnlinkChatsView(discord.ui.View):
    """Список привязанных чатов: множественный выбор + «Отвязать»."""

    def __init__(self, cog: "TelegramLinkCog", author_id: int, links: list):
        super().__init__(timeout=600)
        self.cog = cog
        self.author_id = author_id
        self.selected_ids: list[int] = []
        self.message: Optional[discord.Message] = None
        self._finished = False

        options = [
            discord.SelectOption(label=_chat_label(row)[:100], value=str(row["telegram_chat_id"]))
            for row in links[:25]
        ]
        self.select = discord.ui.Select(
            placeholder=texts.TG_CHATS_SELECT,
            min_values=1,
            max_values=len(options),
            options=options,
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

        unlink = discord.ui.Button(
            label=texts.TG_CHATS_UNLINK_BUTTON, style=discord.ButtonStyle.danger
        )
        unlink.callback = self._on_unlink
        self.add_item(unlink)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    async def _on_select(self, interaction: discord.Interaction) -> None:
        self.selected_ids = [int(v) for v in self.select.values]
        await interaction.response.defer()

    async def _on_unlink(self, interaction: discord.Interaction) -> None:
        if not self.selected_ids:
            await respond(interaction, texts.TG_CHATS_NOTHING_SELECTED,
                          colour=texts.EMBED_COLOR_WARNING)
            return
        self._finished = True
        self.stop()
        await self.cog.unlink_chats(interaction, self.selected_ids)

    async def on_timeout(self) -> None:
        if not self._finished:
            await expire_view_message(self.message)


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

    @app_commands.command(
        name="telegram-chats",
        description="Привязанные Telegram-чаты и их отвязка (админ)",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def telegram_chats_command(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction):
            await deny_admin(interaction)
            return

        links = await self.repo.get_telegram_links(interaction.guild.id)
        if not links:
            await respond(interaction, texts.TG_CHATS_EMPTY, title=texts.TG_CHATS_TITLE)
            return

        chats = "\n".join(f"• {_chat_label(row)}" for row in links)
        view = UnlinkChatsView(self, interaction.user.id, links)
        embed = discord.Embed(
            title=texts.TG_CHATS_TITLE,
            description=texts.TG_CHATS_LIST.format(chats=chats),
            colour=texts.EMBED_COLOR,
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @app_commands.command(
        name="telegram-me",
        description="Подтвердить свой Telegram, чтобы нажимать кнопки в уведомлениях (админ)",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def telegram_me_command(self, interaction: discord.Interaction) -> None:
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
            kind=CODE_KIND_ADMIN,
        )
        embed = discord.Embed(
            title=texts.TG_ME_TITLE,
            description=texts.TG_ME_CODE_TEXT.format(
                code=code, minutes=self.config.connect_code_ttl // 60
            ),
            colour=texts.EMBED_COLOR,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def unlink_chats(self, interaction: discord.Interaction, chat_ids: list[int]) -> None:
        guild_id = interaction.guild.id
        count = 0
        for chat_id in chat_ids:
            if await self.repo.remove_telegram_link(guild_id, chat_id):
                count += 1
                await self.repo.add_action_log(
                    guild_id, "telegram_unlinked", f"chat_id={chat_id} by={interaction.user.id}"
                )
        embed = discord.Embed(
            title=texts.TG_CHATS_TITLE,
            description=texts.TG_CHATS_UNLINKED.format(count=count),
            colour=texts.EMBED_COLOR_SUCCESS,
        )
        await interaction.response.edit_message(embed=embed, view=None)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TelegramLinkCog(bot))
