"""Сводка голосовых каналов в Telegram: одно сообщение, которое бот редактирует.

По команде /online в Telegram-чате бот отправляет сводку по всем серверам,
привязанным к чату: кто в каких каналах, сколько сидит, кто заглушён
(сам или администратором), кто стримит. Дальше это же сообщение
редактируется при каждом изменении голосовых состояний (с дебаунсом —
у Telegram лимиты на редактирование) и раз в минуту, чтобы шло время.
/online_off удаляет сводку.
"""

import asyncio
import logging
import time
from typing import Optional

import discord
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from discord.ext import commands, tasks

from bot.discord_bot.module_cog import ModuleCog
from bot.modules.voice_dashboard import texts
from bot.modules.voice_dashboard.models import NAMESPACE
from bot.modules.voice_dashboard.repository import VoiceDashboardRepository
from bot.telegram_bot.notifier import tg_escape

log = logging.getLogger(__name__)

Key = tuple[int, int]  # (guild_id, user_id)

# Пауза перед редактированием: события за это время склеиваются в одну правку.
REFRESH_DELAY = 3.0
# Лимит Telegram — 4096 символов; держим запас под легенду и HTML.
MESSAGE_LIMIT = 3900


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return texts.DURATION_LESS_MINUTE
    minutes = int(seconds // 60)
    if minutes < 60:
        return texts.DURATION_MINUTES.format(minutes=minutes)
    return texts.DURATION_HOURS.format(hours=minutes // 60, minutes=minutes % 60)


def format_states(voice: discord.VoiceState) -> str:
    """Значки состояния: «навешенное» администратором отличается от своего."""
    icons = []
    # Самоотключение звука в Discord всегда включает и самоотключение
    # микрофона — показываем только звук, как это делает сам Discord.
    if voice.mute:
        icons.append("🔇")
    elif voice.self_mute and not voice.self_deaf:
        icons.append("🙊")
    if voice.deaf:
        icons.append("🔕")
    elif voice.self_deaf:
        icons.append("🙉")
    if voice.self_stream:
        icons.append("📺")
    if voice.self_video:
        icons.append("📷")
    return (" " + "".join(icons)) if icons else ""


def _voice_channels(guild: discord.Guild) -> list[discord.abc.GuildChannel]:
    channels = list(guild.voice_channels) + list(guild.stage_channels)
    return sorted(
        channels,
        key=lambda c: (c.category.position if c.category else -1, c.position, c.id),
    )


class VoiceDashboardCog(ModuleCog):
    module_name = NAMESPACE

    def __init__(self, bot: commands.Bot):
        super().__init__(bot)
        self.repo = VoiceDashboardRepository(bot.db)
        # Когда участник зашёл в текущий канал (в памяти; после рестарта
        # время неизвестно — показываем «≥» от запуска бота).
        self.joined: dict[Key, float] = {}
        self.started_at = time.time()
        self._pending: dict[int, asyncio.Task] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self.periodic_refresh.start()

    def cog_unload(self) -> None:
        self.periodic_refresh.cancel()
        for task in self._pending.values():
            task.cancel()
        self._pending.clear()

    def _lock(self, chat_id: int) -> asyncio.Lock:
        if chat_id not in self._locks:
            self._locks[chat_id] = asyncio.Lock()
        return self._locks[chat_id]

    # ------------------------------------------------------------------
    # Отрисовка
    # ------------------------------------------------------------------
    async def chat_guilds(self, chat_id: int) -> list[discord.Guild]:
        """Серверы, привязанные к чату, на которых модуль включён."""
        guilds = []
        for guild_id in await self.repo.get_linked_guild_ids(chat_id):
            guild = self.bot.get_guild(guild_id)
            if guild is not None and await self.module_enabled(guild_id):
                guilds.append(guild)
        return guilds

    def _duration(self, guild_id: int, member_id: int, now: float) -> str:
        joined = self.joined.get((guild_id, member_id))
        if joined is not None:
            return format_duration(now - joined)
        return texts.DURATION_AT_LEAST.format(duration=format_duration(now - self.started_at))

    def render_guild(self, guild: discord.Guild, now: float) -> list[str]:
        lines = [texts.DASHBOARD_GUILD.format(guild_name=tg_escape(guild.name))]
        empty = True
        for channel in _voice_channels(guild):
            members = [m for m in channel.members if m.voice is not None]
            if not members:
                continue
            empty = False
            lines.append(texts.DASHBOARD_CHANNEL.format(
                channel=tg_escape(channel.name), count=len(members)
            ))
            for member in sorted(members, key=lambda m: m.display_name.casefold()):
                name = tg_escape(member.display_name)
                lines.append(texts.DASHBOARD_MEMBER.format(
                    member=f"🤖 {name}" if member.bot else name,
                    duration=self._duration(guild.id, member.id, now),
                    states=format_states(member.voice),
                ))
        if empty:
            lines.append(texts.DASHBOARD_EMPTY)
        return lines

    async def render(self, chat_id: int) -> Optional[str]:
        """Текст сводки для чата; None — показывать нечего (нет серверов)."""
        guilds = await self.chat_guilds(chat_id)
        if not guilds:
            return None
        now = time.time()
        lines = [texts.DASHBOARD_TITLE]
        for guild in guilds:
            lines.append("")
            lines.extend(self.render_guild(guild, now))

        footer = "\n\n" + texts.DASHBOARD_LEGEND
        body, length = [], len(footer)
        for line in lines:
            if length + len(line) + 1 > MESSAGE_LIMIT:
                body.append(texts.DASHBOARD_TRUNCATED)
                break
            body.append(line)
            length += len(line) + 1
        return "\n".join(body) + footer

    # ------------------------------------------------------------------
    # Отправка / редактирование / удаление
    # ------------------------------------------------------------------
    async def send_dashboard(self, chat_id: int) -> bool:
        """Отправляет новую сводку (старую удаляет). False — нечего показывать."""
        tg_bot = self.bot.tg_bot
        async with self._lock(chat_id):
            text = await self.render(chat_id)
            if text is None:
                return False
            old_message_id = await self.repo.get_dashboard(chat_id)
            if old_message_id is not None:
                try:
                    await tg_bot.delete_message(chat_id, old_message_id)
                except TelegramAPIError:
                    pass  # старую уже удалили руками — не страшно
            message = await tg_bot.send_message(chat_id, text, parse_mode="HTML")
            await self.repo.set_dashboard(chat_id, message.message_id)
            return True

    async def remove_dashboard(self, chat_id: int) -> bool:
        """Удаляет сводку чата. False — её и не было."""
        task = self._pending.pop(chat_id, None)
        if task is not None:
            task.cancel()
        async with self._lock(chat_id):
            message_id = await self.repo.get_dashboard(chat_id)
            if message_id is None:
                return False
            try:
                await self.bot.tg_bot.delete_message(chat_id, message_id)
            except TelegramAPIError:
                pass
            await self.repo.delete_dashboard(chat_id)
            return True

    def schedule_refresh(self, chat_id: int) -> None:
        """Отложенное обновление: все события за REFRESH_DELAY — одна правка."""
        task = self._pending.get(chat_id)
        if task is not None and not task.done():
            return
        self._pending[chat_id] = asyncio.create_task(
            self._delayed_refresh(chat_id), name=f"voice-dashboard-{chat_id}"
        )

    async def _delayed_refresh(self, chat_id: int) -> None:
        try:
            await asyncio.sleep(REFRESH_DELAY)
            # Снимаем отметку до правки: события во время правки
            # запланируют следующую.
            if self._pending.get(chat_id) is asyncio.current_task():
                self._pending.pop(chat_id, None)
            await self.refresh(chat_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Не удалось обновить сводку голосовых каналов в чате %s", chat_id)

    async def refresh(self, chat_id: int) -> None:
        tg_bot = self.bot.tg_bot
        if tg_bot is None:
            return
        async with self._lock(chat_id):
            message_id = await self.repo.get_dashboard(chat_id)
            if message_id is None:
                return
            text = await self.render(chat_id)
            if text is None:
                # Чат отвязали от всех серверов (или модуль везде выключили).
                try:
                    await tg_bot.delete_message(chat_id, message_id)
                except TelegramAPIError:
                    pass
                await self.repo.delete_dashboard(chat_id)
                return
            for _ in range(2):
                try:
                    await tg_bot.edit_message_text(
                        text, chat_id=chat_id, message_id=message_id, parse_mode="HTML"
                    )
                    return
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(exc.retry_after)
                except TelegramBadRequest as exc:
                    reason = str(exc).lower()
                    if "not modified" in reason:
                        return
                    if "not found" in reason or "can't be edited" in reason:
                        # Сводку удалили руками — больше её не ведём.
                        await self.repo.delete_dashboard(chat_id)
                        return
                    log.warning("Не удалось обновить сводку в чате %s: %s", chat_id, exc)
                    return
                except TelegramForbiddenError:
                    await self.repo.delete_dashboard(chat_id)
                    return

    async def refresh_guild(self, guild_id: int) -> None:
        """Планирует обновление сводок во всех чатах, привязанных к серверу."""
        dashboards = await self.repo.list_dashboards()
        if not dashboards:
            return
        for chat_id in await self.repo.get_telegram_chat_ids(guild_id):
            if chat_id in dashboards:
                self.schedule_refresh(chat_id)

    # ------------------------------------------------------------------
    # События Discord
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        key = (member.guild.id, member.id)
        if after.channel is None:
            self.joined.pop(key, None)
        elif before.channel is None or before.channel.id != after.channel.id:
            self.joined[key] = time.time()
        if await self.module_enabled(member.guild.id):
            await self.refresh_guild(member.guild.id)

    @tasks.loop(seconds=60)
    async def periodic_refresh(self) -> None:
        # Раз в минуту — чтобы шли длительности (и подхватить сводки после рестарта).
        for chat_id in await self.repo.list_dashboards():
            self.schedule_refresh(chat_id)

    @periodic_refresh.before_loop
    async def _before_periodic(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VoiceDashboardCog(bot))
