"""Уведомления в Telegram о заходах в голосовые каналы.

Когда участник заходит в голосовой канал (не переход между каналами),
бот сообщает в привязанные Telegram-чаты, кто и куда зашёл. Перезаходы
не спамят: не чаще одного уведомления на участника за
VOICE_NOTIFY_COOLDOWN. О собственных заходах администратора бот не пишет
в чаты, которые тот подключил (/connect). Включение, режим и список отслеживаемых участников
настраиваются только в Telegram (/join_alerts, см. telegram.py).
"""

import logging
import time

import discord
from discord.ext import commands

from bot.discord_bot.module_cog import ModuleCog
from bot.modules.voice_notify import texts
from bot.modules.voice_notify.models import MODE_SELECTED, NAMESPACE
from bot.modules.voice_notify.repository import VoiceNotifyRepository
from bot.telegram_bot.notifier import notify_guild, tg_escape, tg_user

log = logging.getLogger(__name__)

Key = tuple[int, int]  # (guild_id, user_id)


class VoiceNotifyCog(ModuleCog):
    module_name = NAMESPACE

    def __init__(self, bot: commands.Bot):
        super().__init__(bot)
        self.repo = VoiceNotifyRepository(bot.db)
        # Когда последний раз сообщали о заходе участника (антиспам, в памяти).
        self.last_notified: dict[Key, float] = {}

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
        # В чаты, которые подключил сам вошедший, о его заходе не пишем.
        await notify_guild(
            self.bot.tg_bot, self.repo, guild.id, guild.name, text, skip_discord_user=member.id
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VoiceNotifyCog(bot))
