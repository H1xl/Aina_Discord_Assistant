"""Уведомления в Telegram о заходах в голосовые каналы.

Когда участник заходит в голосовой канал (не переход между каналами),
бот сообщает в привязанные Telegram-чаты, кто и куда зашёл. Перезаходы
не спамят: если участник вернулся в голос раньше, чем через
VOICE_NOTIFY_COOLDOWN после выхода, о заходе не сообщаем. О собственных
заходах администратора бот не пишет в чаты, которые тот подключил (/connect).

Автоудаление: если оно включено, отправленные уведомления запоминаются и
удаляются через заданное время после того, как в канале не осталось людей
(новый заход в канал отменяет отсчёт).

Включение, режим, список отслеживаемых и автоудаление настраиваются только
в Telegram (/join_alerts, см. telegram.py).
"""

import logging
import time
from typing import Optional

import discord
from discord.ext import commands, tasks

from bot.discord_bot.module_cog import ModuleCog
from bot.modules.voice_notify import texts
from bot.modules.voice_notify.models import MODE_SELECTED, NAMESPACE
from bot.modules.voice_notify.repository import VoiceNotifyRepository
from bot.telegram_bot.notifier import notify_guild, tg_escape, tg_user

log = logging.getLogger(__name__)

Key = tuple[int, int]  # (guild_id, user_id) или (guild_id, channel_id)


def _has_people(channel) -> bool:
    return any(not member.bot for member in channel.members)


class VoiceNotifyCog(ModuleCog):
    module_name = NAMESPACE

    def __init__(self, bot: commands.Bot):
        super().__init__(bot)
        self.repo = VoiceNotifyRepository(bot.db)
        # Когда участник последний раз вышел из голоса (антиспам перезаходов,
        # в памяти; после рестарта первый заход просто будет сообщён).
        self.last_left: dict[Key, float] = {}
        # Каналы, по которым есть уведомления к удалению, — чтобы не ходить
        # в базу на каждое голосовое событие.
        self.pending_channels: set[Key] = set()
        self.autodelete_loop.start()

    def cog_unload(self) -> None:
        self.autodelete_loop.cancel()

    # ------------------------------------------------------------------
    # Голосовые события
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        if member.bot or before.channel == after.channel:
            return
        guild = member.guild
        await self._track_emptiness(guild.id, before.channel, after.channel)

        key = (guild.id, member.id)
        cooldown = self.config.voice_notify_cooldown
        now = time.monotonic()
        # Выход из голоса — запоминаем время, от него считается задержка.
        if before.channel is not None and after.channel is None:
            self._remember_leave(key, now, cooldown)
            return
        # Дальше интересен только вход: переходы между каналами пропускаем.
        if before.channel is not None:
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
            name=tg_escape(member.display_name),
            member=tg_user(member.display_name, member.name),
            channel=tg_escape(channel.name),
            count=len(channel.members),
        ) + texts.TG_JOIN_COOLDOWN.format(minutes=cooldown // 60)
        await self.repo.add_action_log(
            guild.id, "voice_join_notified", f"user_id={member.id} channel_id={channel.id}"
        )
        # В чаты, которые подключил сам вошедший, о его заходе не пишем.
        sent = await notify_guild(
            self.bot.tg_bot, self.repo, guild.id, guild.name, text, skip_discord_user=member.id
        )
        if sent and await self.repo.get_autodelete(guild.id):
            await self.repo.add_messages(guild.id, channel.id, sent)
            self.pending_channels.add((guild.id, channel.id))

    def _remember_leave(self, key: Key, now: float, cooldown: int) -> None:
        self.last_left[key] = now
        # Устаревшие записи больше ни на что не влияют — не копим их.
        if len(self.last_left) > 500:
            self.last_left = {k: t for k, t in self.last_left.items() if now - t < cooldown}

    # ------------------------------------------------------------------
    # Автоудаление
    # ------------------------------------------------------------------
    async def _track_emptiness(
        self, guild_id: int, left_channel: Optional[discord.abc.GuildChannel],
        joined_channel: Optional[discord.abc.GuildChannel],
    ) -> None:
        """Запускает или отменяет отсчёт удаления для каналов с уведомлениями."""
        if joined_channel is not None and (guild_id, joined_channel.id) in self.pending_channels:
            await self.repo.unschedule_channel(guild_id, joined_channel.id)
        if (
            left_channel is not None
            and (guild_id, left_channel.id) in self.pending_channels
            and not _has_people(left_channel)
        ):
            await self._schedule(guild_id, left_channel.id)

    async def _schedule(self, guild_id: int, channel_id: int) -> None:
        minutes = await self.repo.get_autodelete(guild_id)
        if minutes:
            await self.repo.schedule_channel(guild_id, channel_id, int(time.time()) + minutes * 60)

    async def reload_pending(self) -> None:
        """Перечитывает каналы с уведомлениями к удалению (после смены настройки)."""
        rows = await self.repo.channels_with_messages()
        self.pending_channels = {(row["guild_id"], row["channel_id"]) for row in rows}

    @tasks.loop(minutes=1)
    async def autodelete_loop(self) -> None:
        rows = await self.repo.pop_due_messages()
        for row in rows:
            try:
                await self.bot.tg_bot.delete_message(row["chat_id"], row["message_id"])
            except Exception as exc:
                # Сообщение уже удалили вручную или бота убрали из чата.
                log.info("Не удалось удалить уведомление %s/%s: %s",
                         row["chat_id"], row["message_id"], exc)
        if rows:
            await self.reload_pending()

    @autodelete_loop.before_loop
    async def _before_autodelete(self) -> None:
        await self.bot.wait_until_ready()
        await self.reload_pending()
        # Каналы, опустевшие, пока бот был выключен: назначаем отсчёт сейчас.
        for row in await self.repo.channels_with_messages():
            if row["delete_at"] is not None:
                continue
            channel = self.bot.get_channel(row["channel_id"])
            if channel is None or not _has_people(channel):
                await self._schedule(row["guild_id"], row["channel_id"])


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VoiceNotifyCog(bot))
