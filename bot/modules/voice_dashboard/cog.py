"""Сводка голосовых каналов в Telegram: по сообщению на сервер, которое бот редактирует.

По команде /online в Telegram-чате бот отправляет отдельную сводку на каждый
сервер, привязанный к чату: кто в каких каналах, сколько сидит, кто заглушён
(сам или администратором), кто стримит. Дальше это же сообщение
редактируется при каждом изменении голосовых состояний этого сервера (с дебаунсом —
у Telegram лимиты на редактирование) и раз в минуту, чтобы шло время.
/online_off удаляет сводки чата.
"""

import asyncio
import logging
import time

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
from bot.modules.voice_dashboard.repository import DashboardKey, VoiceDashboardRepository
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
        self._pending: dict[DashboardKey, asyncio.Task] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._legacy_done = False
        self.periodic_refresh.start()

    def cog_unload(self) -> None:
        self.periodic_refresh.cancel()
        for task in self._pending.values():
            task.cancel()
        self._pending.clear()

    def _lock(self, chat_id: int) -> asyncio.Lock:
        """Лок на чат: отправка, правка и удаление его сводок не пересекаются."""
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

    async def _shown_in_chat(self, chat_id: int, guild_id: int) -> bool:
        """Сводку сервера в чате ещё стоит вести (сервер привязан, модуль включён)."""
        return (
            self.bot.get_guild(guild_id) is not None
            and guild_id in await self.repo.get_linked_guild_ids(chat_id)
            and await self.module_enabled(guild_id)
        )

    def _duration(self, guild_id: int, member_id: int, now: float) -> str:
        joined = self.joined.get((guild_id, member_id))
        if joined is not None:
            return format_duration(now - joined)
        return texts.DURATION_AT_LEAST.format(duration=format_duration(now - self.started_at))

    def render(self, guild: discord.Guild) -> str:
        """Текст сводки одного сервера."""
        now = time.time()
        lines = [texts.DASHBOARD_TITLE.format(guild_name=tg_escape(guild.name)), ""]
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
    async def _delete_message(self, chat_id: int, message_id: int) -> None:
        try:
            await self.bot.tg_bot.delete_message(chat_id, message_id)
        except TelegramAPIError:
            pass  # сообщение уже удалили руками или бот потерял доступ

    def _cancel_pending(self, chat_id: int) -> None:
        for key in [k for k in self._pending if k[0] == chat_id]:
            self._pending.pop(key).cancel()

    async def send_dashboards(self, chat_id: int) -> bool:
        """Присылает по сводке на каждый сервер чата (старые удаляет).

        False — показывать нечего (нет привязанных серверов с включённым модулем).
        """
        self._cancel_pending(chat_id)
        async with self._lock(chat_id):
            guilds = await self.chat_guilds(chat_id)
            if not guilds:
                return False
            for guild_id, message_id in (await self.repo.chat_dashboards(chat_id)).items():
                await self._delete_message(chat_id, message_id)
                await self.repo.delete_dashboard(chat_id, guild_id)
            for guild in guilds:
                message = await self.bot.tg_bot.send_message(
                    chat_id, self.render(guild), parse_mode="HTML"
                )
                await self.repo.set_dashboard(chat_id, guild.id, message.message_id)
            return True

    async def remove_dashboards(self, chat_id: int) -> bool:
        """Удаляет все сводки чата. False — их и не было."""
        self._cancel_pending(chat_id)
        async with self._lock(chat_id):
            dashboards = await self.repo.chat_dashboards(chat_id)
            for guild_id, message_id in dashboards.items():
                await self._delete_message(chat_id, message_id)
                await self.repo.delete_dashboard(chat_id, guild_id)
            return bool(dashboards)

    def schedule_refresh(self, chat_id: int, guild_id: int) -> None:
        """Отложенное обновление: все события за REFRESH_DELAY — одна правка."""
        key = (chat_id, guild_id)
        task = self._pending.get(key)
        if task is not None and not task.done():
            return
        self._pending[key] = asyncio.create_task(
            self._delayed_refresh(chat_id, guild_id), name=f"voice-dashboard-{chat_id}-{guild_id}"
        )

    async def _delayed_refresh(self, chat_id: int, guild_id: int) -> None:
        key = (chat_id, guild_id)
        try:
            await asyncio.sleep(REFRESH_DELAY)
            # Снимаем отметку до правки: события во время правки
            # запланируют следующую.
            if self._pending.get(key) is asyncio.current_task():
                self._pending.pop(key, None)
            await self.refresh(chat_id, guild_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Не удалось обновить сводку сервера %s в чате %s", guild_id, chat_id)

    async def refresh(self, chat_id: int, guild_id: int) -> None:
        tg_bot = self.bot.tg_bot
        if tg_bot is None:
            return
        async with self._lock(chat_id):
            message_id = await self.repo.get_dashboard(chat_id, guild_id)
            if message_id is None:
                return
            if not await self._shown_in_chat(chat_id, guild_id):
                # Сервер отвязали от чата или выключили модуль — сводку убираем.
                await self._delete_message(chat_id, message_id)
                await self.repo.delete_dashboard(chat_id, guild_id)
                return
            text = self.render(self.bot.get_guild(guild_id))
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
                        await self.repo.delete_dashboard(chat_id, guild_id)
                        return
                    log.warning("Не удалось обновить сводку в чате %s: %s", chat_id, exc)
                    return
                except TelegramForbiddenError:
                    await self.repo.delete_dashboard(chat_id, guild_id)
                    return

    async def refresh_guild(self, guild_id: int) -> None:
        """Планирует обновление сводки сервера во всех чатах, где она есть."""
        for chat_id in await self.repo.guild_dashboard_chats(guild_id):
            self.schedule_refresh(chat_id, guild_id)

    async def _migrate_legacy(self) -> None:
        """Общие сводки до v2 заменяем раздельными: старое сообщение удаляем,
        в чат присылаем новые — по одной на сервер."""
        for chat_id, message_id in (await self.repo.pop_legacy_dashboards()).items():
            async with self._lock(chat_id):
                await self._delete_message(chat_id, message_id)
            try:
                await self.send_dashboards(chat_id)
            except TelegramAPIError as exc:
                log.info("Не удалось пересоздать сводку в чате %s: %s", chat_id, exc)

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
        if not self._legacy_done:
            self._legacy_done = True
            await self._migrate_legacy()
        # Раз в минуту — чтобы шли длительности (и подхватить сводки после рестарта).
        for chat_id, guild_id in await self.repo.list_dashboards():
            self.schedule_refresh(chat_id, guild_id)

    @periodic_refresh.before_loop
    async def _before_periodic(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VoiceDashboardCog(bot))
