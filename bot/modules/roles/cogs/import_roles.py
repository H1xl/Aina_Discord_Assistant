"""/import-roles — привязка к участникам уже существующих именных ролей.

Роли, созданные вручную или выданные до появления бота, не числятся
в personal_roles, и их владельцы не могут менять их через /role.
Команда сопоставляет выбранные роли с носителями и сохраняет привязки.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.discord_bot.helpers import deny_admin, is_admin, with_backoff
from bot.discord_bot.module_cog import ModuleCog
from bot.modules.roles import models, texts
from bot.modules.roles.repository import RolesRepository
from bot.modules.roles.views.import_roles import ImportRolesView, RoleEntry, analyze_roles

log = logging.getLogger(__name__)


class ImportRolesCog(ModuleCog):
    module_name = "roles"

    def __init__(self, bot: commands.Bot):
        super().__init__(bot)
        self.repo = RolesRepository(bot.db)

    @app_commands.command(
        name="import-roles",
        description="Привязать к участникам существующие именные роли (админ)",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def import_roles_command(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction):
            await deny_admin(interaction)
            return
        view = ImportRolesView(self, interaction.user.id)
        await interaction.response.send_message(
            embed=view.pick_embed(), view=view, ephemeral=True
        )
        view.message = await interaction.original_response()

    async def analyze(
        self, guild: discord.Guild, roles: list[discord.Role]
    ) -> tuple[list[RoleEntry], dict[int, int]]:
        """Возвращает разбор ролей и текущие привязки user_id → role_id."""
        bound = await self.repo.get_personal_roles(guild.id)
        reserved = set(await self.repo.get_mandatory_roles(guild.id))
        reserved |= set(await self.repo.get_optional_roles(guild.id))
        settings = await self.repo.get_settings(guild.id)
        if settings and settings["no_role_id"]:
            reserved.add(settings["no_role_id"])
        entries = analyze_roles(guild, roles, bound, reserved)
        user_roles = {user_id: role_id for role_id, user_id in bound.items()}
        return entries, user_roles

    async def save(self, interaction: discord.Interaction, plan: list[RoleEntry]) -> None:
        # Выдача ролей может занять время — сначала подтверждаем interaction.
        await interaction.response.defer()
        guild = interaction.guild

        failed = []
        bindings = []
        for entry in plan:
            if entry.grants_role:
                try:
                    await entry.owner.add_roles(
                        entry.role, reason=f"Импорт именной роли ({interaction.user})"
                    )
                except discord.HTTPException:
                    failed.append(entry.owner.mention)
                    continue
            bindings.append(entry)

        await self.repo.bind_personal_roles(
            guild.id,
            [(entry.owner.id, entry.role.id) for entry in bindings],
            models.SOURCE_IMPORT,
        )
        for entry in bindings:
            await self.repo.add_action_log(
                guild.id, "role_import",
                f"user_id={entry.owner.id} role_id={entry.role.id} by={interaction.user.id}",
            )

        if bindings:
            await self._log_import(guild, interaction.user, bindings)

        description = texts.IMPORT_SAVED.format(count=len(bindings))
        if failed:
            description += texts.IMPORT_GRANT_FAILED.format(members=", ".join(failed))
        embed = discord.Embed(
            title=texts.IMPORT_TITLE,
            description=description,
            colour=texts.EMBED_COLOR_WARNING if failed else texts.EMBED_COLOR_SUCCESS,
        )
        await interaction.edit_original_response(embed=embed, view=None)

    async def _log_import(
        self, guild: discord.Guild, admin: discord.abc.User, bindings: list[RoleEntry]
    ) -> None:
        settings = await self.repo.get_settings(guild.id)
        channel = guild.get_channel(settings["log_channel_id"]) if settings else None
        if not isinstance(channel, discord.TextChannel):
            return
        lines = "\n".join(f"• {e.role.mention} → {e.owner.mention}" for e in bindings)
        embed = discord.Embed(
            title=texts.LOG_IMPORT_TITLE,
            description=texts.LOG_IMPORT_TEXT.format(admin=admin.mention, lines=lines)[:4000],
            colour=texts.EMBED_COLOR,
        )
        try:
            await with_backoff(lambda: channel.send(embed=embed))
        except discord.Forbidden:
            log.warning("Нет прав писать в журнал сервера %s", guild.id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ImportRolesCog(bot))
