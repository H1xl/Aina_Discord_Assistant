"""Команды настройки сервера: /setup, /setup-optional-roles, /stats."""

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from bot.discord_bot.helpers import deny_admin
from bot.modules.roles import texts
from bot.modules.roles.repository import RolesRepository
from bot.modules.roles.views.selects import (
    OptionalRolesSetupView,
    SetupChannelsView,
    SetupRolesView,
)

log = logging.getLogger(__name__)


class SetupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.repo = RolesRepository(bot.db)
        self.config = bot.config

    # Discord modal поддерживает только текстовые поля, поэтому /setup —
    # это ephemeral-сообщение с ChannelSelect/RoleSelect. В один View
    # помещается максимум 5 строк, поэтому настройка идёт в два шага:
    # каналы («Далее») → роли («Сохранить»).
    @app_commands.command(
        name="setup", description="Настроить каналы и роли верификации (админ)"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def setup_command(self, interaction: discord.Interaction) -> None:
        if not interaction.user.guild_permissions.administrator:
            await deny_admin(interaction)
            return

        view = SetupChannelsView(author_id=interaction.user.id)
        view._next_callback = self._show_roles_step
        embed = discord.Embed(
            title=texts.SETUP_TITLE,
            description=texts.SETUP_PROMPT,
            colour=texts.EMBED_COLOR,
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

    async def _show_roles_step(
        self, interaction: discord.Interaction, channels_view: SetupChannelsView
    ) -> None:
        view = SetupRolesView(channels_view)
        view._save_callback = self._save_settings
        embed = discord.Embed(
            title=texts.SETUP_TITLE,
            description=texts.SETUP_ROLES_PROMPT,
            colour=texts.EMBED_COLOR,
        )
        await interaction.response.edit_message(embed=embed, view=view)

    async def _save_settings(
        self, interaction: discord.Interaction, view: SetupRolesView
    ) -> None:
        guild = interaction.guild
        await interaction.response.defer(ephemeral=True)

        # Роль «Не имеет ролей»: выбранная админом; иначе сохранённая,
        # по имени, иначе создаём новую.
        picked = (
            guild.get_role(view.no_role_pick_id) if view.no_role_pick_id else None
        )
        settings = await self.repo.get_settings(guild.id)
        no_role = picked
        if no_role is None and settings and settings["no_role_id"]:
            no_role = guild.get_role(settings["no_role_id"])
        if no_role is None:
            no_role = discord.utils.get(guild.roles, name=texts.NO_ROLE_NAME)

        created = False
        if no_role is None:
            try:
                no_role = await guild.create_role(
                    name=texts.NO_ROLE_NAME,
                    colour=discord.Colour.light_grey(),
                    reason="Служебная роль верификации (/setup)",
                )
                created = True
            except discord.Forbidden:
                await interaction.followup.send(
                    embed=discord.Embed(
                        description=texts.SETUP_ROLE_HIERARCHY_WARNING,
                        colour=texts.EMBED_COLOR_ERROR,
                    ),
                    ephemeral=True,
                )
                return

        await self.repo.upsert_settings(
            guild.id,
            roles_channel_id=view.roles_channel_id,
            pings_channel_id=view.pings_channel_id,
            log_channel_id=view.log_channel_id,
            no_role_id=no_role.id,
        )
        await self.repo.set_mandatory_roles(guild.id, view.mandatory_role_ids)
        await self.repo.add_action_log(
            guild.id, "setup", f"by={interaction.user.id}"
        )

        if picked is not None:
            no_role_state = texts.SETUP_NO_ROLE_PICKED
        elif created:
            no_role_state = texts.SETUP_NO_ROLE_CREATED
        else:
            no_role_state = texts.SETUP_NO_ROLE_EXISTS
        description = texts.SETUP_SAVED.format(
            no_role=no_role.name,
            no_role_state=no_role_state,
        )
        # Предупреждаем, если роль бота ниже выдаваемых ролей.
        top = guild.me.top_role
        issued = [guild.get_role(rid) for rid in view.mandatory_role_ids]
        if any(role and role >= top for role in issued) or no_role >= top:
            description += "\n\n" + texts.SETUP_ROLE_HIERARCHY_WARNING

        embed = discord.Embed(
            title=texts.SETUP_TITLE,
            description=description,
            colour=texts.EMBED_COLOR_SUCCESS,
        )
        try:
            await view.message.edit(embed=embed, view=None)
        except discord.HTTPException:
            await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    @app_commands.command(
        name="setup-optional-roles",
        description="Задать пул дополнительных ролей для новичков (админ)",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def setup_optional_roles(self, interaction: discord.Interaction) -> None:
        if not interaction.user.guild_permissions.administrator:
            await deny_admin(interaction)
            return

        view = OptionalRolesSetupView(author_id=interaction.user.id)
        view._save_callback = self._save_optional_roles
        embed = discord.Embed(
            title=texts.OPTIONAL_ROLES_TITLE,
            description=texts.OPTIONAL_SETUP_PROMPT,
            colour=texts.EMBED_COLOR,
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

    async def _save_optional_roles(
        self, interaction: discord.Interaction, view: OptionalRolesSetupView
    ) -> None:
        guild = interaction.guild
        await self.repo.set_optional_roles(guild.id, view.role_ids)
        await self.repo.add_action_log(
            guild.id, "setup_optional", f"count={len(view.role_ids)}"
        )
        embed = discord.Embed(
            description=texts.OPTIONAL_SETUP_SAVED.format(count=len(view.role_ids)),
            colour=texts.EMBED_COLOR_SUCCESS,
        )
        await interaction.response.edit_message(embed=embed, view=None)

    # ------------------------------------------------------------------
    @app_commands.command(
        name="stats", description="Статистика верификаций за период (админ)"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(days="Период в днях (по умолчанию 7)")
    async def stats_command(
        self, interaction: discord.Interaction, days: app_commands.Range[int, 1, 365] = 7
    ) -> None:
        if not interaction.user.guild_permissions.administrator:
            await deny_admin(interaction)
            return
        since = int(time.time()) - days * 86400
        stats = await self.repo.get_stats(interaction.guild.id, since)
        embed = discord.Embed(
            title=texts.STATS_TITLE,
            description=texts.STATS_TEXT.format(
                days=days, completed=stats["completed"], kicked=stats["kicked"]
            ),
            colour=texts.EMBED_COLOR,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SetupCog(bot))
