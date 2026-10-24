"""Уведомления в Telegram о заходах в голосовые каналы.

Когда участник заходит в голосовой канал (не переход между каналами),
бот сообщает в привязанные Telegram-чаты, кто и куда зашёл. Перезаходы
не спамят: не чаще одного уведомления на участника за
VOICE_NOTIFY_COOLDOWN. Можно отслеживать только выбранных участников
(/join-alerts). На сервере модуль по умолчанию включён (/modules).
"""

import logging
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.discord_bot.helpers import deny_admin, expire_view_message, is_admin
from bot.discord_bot.module_cog import ModuleCog
from bot.modules.voice_notify import texts
from bot.modules.voice_notify.models import MODE_ALL, MODE_SELECTED, NAMESPACE
from bot.modules.voice_notify.repository import VoiceNotifyRepository
from bot.telegram_bot.notifier import notify_guild, tg_escape, tg_user

log = logging.getLogger(__name__)

Key = tuple[int, int]  # (guild_id, user_id)


def _mode_title(mode: str) -> str:
    return texts.MODE_SELECTED_LABEL if mode == MODE_SELECTED else texts.MODE_ALL_LABEL


def _users_title(mode: str, user_ids: list[int]) -> str:
    if mode != MODE_SELECTED:
        return texts.USERS_ALL
    return ", ".join(f"<@{uid}>" for uid in user_ids) or texts.USERS_NONE


class VoiceNotifySettingsView(discord.ui.View):
    """Ephemeral-меню /join-alerts: режим + отслеживаемые + «Сохранить»."""

    def __init__(self, cog: "VoiceNotifyCog", author_id: int, mode: str, user_ids: list[int]):
        super().__init__(timeout=600)
        self.cog = cog
        self.author_id = author_id
        self.mode = mode
        self.user_ids = list(user_ids)
        self.message: Optional[discord.Message] = None
        self._finished = False

        self.mode_select = discord.ui.Select(
            placeholder=texts.MODE_PLACEHOLDER,
            options=[
                discord.SelectOption(
                    label=texts.MODE_ALL_LABEL, description=texts.MODE_ALL_HINT,
                    value=MODE_ALL, default=mode == MODE_ALL,
                ),
                discord.SelectOption(
                    label=texts.MODE_SELECTED_LABEL, description=texts.MODE_SELECTED_HINT,
                    value=MODE_SELECTED, default=mode == MODE_SELECTED,
                ),
            ],
            row=0,
        )
        self.mode_select.callback = self._on_mode
        self.add_item(self.mode_select)

        self.users_select = discord.ui.UserSelect(
            placeholder=texts.USERS_PLACEHOLDER,
            min_values=0,
            max_values=25,
            default_values=[
                discord.SelectDefaultValue(id=uid, type=discord.SelectDefaultValueType.user)
                for uid in self.user_ids[:25]
            ],
            row=1,
        )
        self.users_select.callback = self._on_users
        self.add_item(self.users_select)

        save = discord.ui.Button(
            label=texts.SAVE_BUTTON, style=discord.ButtonStyle.success, row=2
        )
        save.callback = self._on_save
        self.add_item(save)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    async def on_timeout(self) -> None:
        if not self._finished:
            await expire_view_message(self.message)

    async def _on_mode(self, interaction: discord.Interaction) -> None:
        self.mode = self.mode_select.values[0]
        await interaction.response.defer()

    async def _on_users(self, interaction: discord.Interaction) -> None:
        self.user_ids = [user.id for user in self.users_select.values if not user.bot]
        await interaction.response.defer()

    async def _on_save(self, interaction: discord.Interaction) -> None:
        self._finished = True
        self.stop()
        await self.cog.save_settings(interaction, self.mode, self.user_ids)


class VoiceNotifyCog(ModuleCog):
    module_name = NAMESPACE

    def __init__(self, bot: commands.Bot):
        super().__init__(bot)
        self.repo = VoiceNotifyRepository(bot.db)
        # Когда последний раз сообщали о заходе участника (антиспам, в памяти).
        self.last_notified: dict[Key, float] = {}

    # ------------------------------------------------------------------
    # /join-alerts
    # ------------------------------------------------------------------
    @app_commands.command(
        name="join-alerts",
        description="Настроить уведомления о заходах в голосовые каналы (админ)",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def voice_notify_command(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction):
            await deny_admin(interaction)
            return
        guild_id = interaction.guild.id
        mode = await self.repo.get_mode(guild_id)
        user_ids = await self.repo.get_tracked_users(guild_id)
        view = VoiceNotifySettingsView(self, interaction.user.id, mode, user_ids)
        embed = discord.Embed(
            title=texts.SETTINGS_TITLE,
            description=texts.SETTINGS_PROMPT.format(
                minutes=self.config.voice_notify_cooldown // 60,
                mode=_mode_title(mode),
                users=_users_title(mode, user_ids),
            ),
            colour=texts.EMBED_COLOR,
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Настраивать фильтр можно и при выключенном модуле — чтобы подготовить
        # его заранее; о выключенном состоянии напоминаем в сводке.
        return True

    async def save_settings(
        self, interaction: discord.Interaction, mode: str, user_ids: list[int]
    ) -> None:
        guild_id = interaction.guild.id
        await self.repo.save_settings(guild_id, mode, user_ids)
        await self.repo.add_action_log(
            guild_id, "voice_notify_settings",
            f"mode={mode} users={len(user_ids)} by={interaction.user.id}",
        )
        description = texts.SAVED.format(
            mode=_mode_title(mode), users=_users_title(mode, user_ids)
        )
        if mode == MODE_SELECTED and not user_ids:
            description += texts.SAVED_EMPTY_WARNING
        if not await self.module_enabled(guild_id):
            description += texts.SETTINGS_DISABLED_HINT
        embed = discord.Embed(
            title=texts.SETTINGS_TITLE, description=description, colour=texts.EMBED_COLOR_SUCCESS
        )
        await interaction.response.edit_message(embed=embed, view=None)

    # ------------------------------------------------------------------
    # Заходы в голосовые каналы
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        # Только вход в голос: переходы между каналами и выходы не интересны.
        if member.bot or before.channel is not None or after.channel is None:
            return
        guild = member.guild
        if not await self.module_enabled(guild.id):
            return
        if await self.repo.get_mode(guild.id) == MODE_SELECTED:
            if member.id not in await self.repo.get_tracked_users(guild.id):
                return

        key = (guild.id, member.id)
        cooldown = self.config.voice_notify_cooldown
        now = time.monotonic()
        last = self.last_notified.get(key)
        if last is not None and now - last < cooldown:
            return
        self.last_notified[key] = now

        channel = after.channel
        text = texts.TG_JOIN_NOTICE.format(
            member=tg_user(member.display_name, member.name),
            channel=tg_escape(channel.name),
            count=len(channel.members),
        ) + texts.TG_JOIN_COOLDOWN.format(minutes=cooldown // 60)
        await self.repo.add_action_log(
            guild.id, "voice_join_notified", f"user_id={member.id} channel_id={channel.id}"
        )
        await notify_guild(self.bot.tg_bot, self.repo, guild.id, guild.name, text)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VoiceNotifyCog(bot))
