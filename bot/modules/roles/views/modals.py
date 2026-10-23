"""Модальные окна /role: первичная верификация и редактирование роли."""

from typing import TYPE_CHECKING

import discord

from bot.modules.roles import texts
from bot.core.utils import parse_hex_color

if TYPE_CHECKING:
    from bot.modules.roles.cogs.verification import VerificationCog


def _error_embed(description: str) -> discord.Embed:
    return discord.Embed(
        title="Ой!", description=description, colour=texts.EMBED_COLOR_ERROR
    )


class RoleModal(discord.ui.Modal):
    """Модалка первичной верификации: название, цвет, кто пригласил."""

    def __init__(self, cog: "VerificationCog", tracked: bool):
        super().__init__(title=texts.MODAL_TITLE, timeout=None)
        self.cog = cog
        # tracked=True — участник числится в pending_verifications
        # (идёт таймер кика); False — старый участник без записи.
        self.tracked = tracked

        self.role_name = discord.ui.TextInput(
            label=texts.FIELD_ROLE_NAME,
            required=True,
            max_length=100,
        )
        self.role_color = discord.ui.TextInput(
            label=texts.FIELD_ROLE_COLOR,
            required=True,
            min_length=6,
            max_length=7,
            placeholder="#AABBCC",
        )
        self.inviter = discord.ui.TextInput(
            label=texts.FIELD_INVITER,
            required=False,
            max_length=100,
            placeholder=texts.FIELD_INVITER_PLACEHOLDER,
        )
        self.add_item(self.role_name)
        self.add_item(self.role_color)
        self.add_item(self.inviter)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        color_value = parse_hex_color(self.role_color.value)
        if color_value is None:
            await interaction.response.send_message(
                embed=_error_embed(
                    texts.HEX_INVALID.format(value=self.role_color.value)
                ),
                ephemeral=True,
            )
            return

        await self.cog.handle_role_modal_submit(
            interaction,
            role_name=self.role_name.value.strip(),
            color_value=color_value,
            inviter=self.inviter.value.strip() or None,
            tracked=self.tracked,
        )


class EditRoleModal(discord.ui.Modal):
    """Модалка редактирования существующей именной роли (имя + цвет)."""

    def __init__(self, cog: "VerificationCog", role: discord.Role):
        super().__init__(title=texts.MODAL_EDIT_TITLE, timeout=None)
        self.cog = cog
        self.role_id = role.id

        self.role_name = discord.ui.TextInput(
            label=texts.FIELD_ROLE_NAME,
            required=True,
            max_length=100,
            default=role.name,
        )
        self.role_color = discord.ui.TextInput(
            label=texts.FIELD_ROLE_COLOR,
            required=True,
            min_length=6,
            max_length=7,
            default=f"#{role.colour.value:06X}",
        )
        self.add_item(self.role_name)
        self.add_item(self.role_color)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        color_value = parse_hex_color(self.role_color.value)
        if color_value is None:
            await interaction.response.send_message(
                embed=_error_embed(
                    texts.HEX_INVALID.format(value=self.role_color.value)
                ),
                ephemeral=True,
            )
            return

        await self.cog.handle_role_edit_submit(
            interaction,
            role_id=self.role_id,
            new_name=self.role_name.value.strip(),
            new_color=color_value,
        )
