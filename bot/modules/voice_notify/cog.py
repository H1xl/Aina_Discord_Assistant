"""Уведомления в Telegram о заходах в голосовые каналы.

Когда участник заходит в голосовой канал (не переход между каналами),
бот сообщает в привязанные Telegram-чаты, кто и куда зашёл. Перезаходы
не спамят: если участник вернулся в голос раньше, чем через
VOICE_NOTIFY_COOLDOWN после выхода, о заходе не сообщаем. О собственных заходах администратора бот не пишет
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
        # Когда участник последний раз вышел из голоса (антиспам перезаходов,
        # в памяти; после рестарта первый заход просто будет сообщён).
        self.last_left: dict[Key, float] = {}

    # ------------------------------------------------------------------
    # Заходы в голосовые каналы
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        if member.bot:
            return
        guild = member.guild
        key = (guild.id, member.id)
        cooldown = self.config.voice_notify_cooldown
        now = time.monotonic()
        # Выход из голоса — запоминаем время, от него считается задержка.
        if before.channel is not None and after.channel is None:
            self._remember_leave(key, now, cooldown)
            return
        # Дальше интересен только вход: переходы между каналами пропускаем.
        if before.channel is not None or after.channel is None:
            return
        left = self.last_left.pop(key, None)
        if left is not None and now - left < cooldown:
            return  # перезаход
        if not await self.module_enabled(guild.id):
            return
        if await self.repo.get_mode(guild.id) == MODE_SELECTED:
            if member.id not in await self.repo.get_tracked_users(guild.id):
                return

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

    def _remember_leave(self, key: Key, now: float, cooldown: int) -> None:
        self.last_left[key] = now
        # Устаревшие записи больше ни на что не влияют — не копим их.
        if len(self.last_left) > 500:
            self.last_left = {k: t for k, t in self.last_left.items() if now - t < cooldown}


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VoiceNotifyCog(bot))
