"""Защита от мута: /protect, /unprotect и автоматическая отмена помех.

От чего защищать, выбирается в /protect (колонка guards):
- mute / deaf — server mute и server deafen снимаются сразу;
- move — если участника перетащил в другой канал кто-то другой, бот
  возвращает его обратно (свои переходы участника не трогаются);
- disconnect — отключение от голоса отменить нельзя, бот только сообщает;
- timeout — тайм-аут снимается сразу.

При включённых уведомлениях бот пишет в привязанные Telegram-чаты, кто
помешал (по журналу аудита), не чаще раза в MUTE_GUARD_NOTIFY_COOLDOWN,
и добавляет кнопки действий (снять/продлить защиту, наказать виновника).
Собственные mute/deafen участника (self_mute/self_deaf) не трогаем.
"""

import asyncio
import datetime as dt
import logging
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.discord_bot.helpers import deny_admin, is_admin, respond
from bot.discord_bot.module_cog import ModuleCog
from bot.modules.mute_guard import texts
from bot.modules.mute_guard.models import (
    GUARD_DEAF,
    GUARD_DISCONNECT,
    GUARD_MOVE,
    GUARD_MUTE,
    GUARD_TIMEOUT,
    NAMESPACE,
    parse_guards,
)
from bot.modules.mute_guard.repository import MuteGuardRepository
from bot.modules.mute_guard.telegram import attempt_keyboard
from bot.modules.mute_guard.views import (
    ProtectView,
    UnprotectView,
    format_guards,
    format_until,
)
from bot.telegram_bot.notifier import notify_guild, tg_escape, tg_user

log = logging.getLogger(__name__)

# Насколько свежей должна быть запись журнала аудита member_update, чтобы
# считать её причиной заглушения/тайм-аута. Discord склеивает повторные
# одинаковые действия одного модератора в одну запись, поэтому окно с запасом.
AUDIT_WINDOW = dt.timedelta(minutes=10)
# Запись в журнале аудита появляется чуть позже события.
AUDIT_DELAY = 1.5
# member_move / member_disconnect: новая (ещё не виденная) запись считается
# причиной события, только если она моложе этого.
AUDIT_FRESH = dt.timedelta(seconds=15)
# Сколько id записей журнала держать в кеше счётчиков на сервер.
AUDIT_CACHE_LIMIT = 300

Key = tuple[int, int]  # (guild_id, user_id)


def _member_display(member: discord.abc.User) -> str:
    """Участник для Telegram-уведомления (HTML)."""
    return tg_user(member.display_name, member.name)


class MuteGuardCog(ModuleCog):
    module_name = NAMESPACE

    def __init__(self, bot: commands.Bot):
        super().__init__(bot)
        self.repo = MuteGuardRepository(bot.db)
        # Сериализуем обработку событий одного участника: при «войне»
        # мута события сыплются пачкой.
        self.locks: dict[Key, asyncio.Lock] = {}
        # Журнал аудита member_move/member_disconnect: записи без target,
        # повторы склеиваются (растёт extra.count). Чтобы понять, что
        # действие только что совершил кто-то, храним последний виденный
        # count каждой записи: guild_id → {entry_id: count}.
        self.audit_counts: dict[int, dict[int, int]] = {}
        # Тайм-ауты, которые бот выдаёт сам (кнопка в Telegram), — их не снимаем.
        self.own_timeouts: set[Key] = set()
        self.cleanup_expired.start()

    def cog_unload(self) -> None:
        self.cleanup_expired.cancel()

    def _lock(self, key: Key) -> asyncio.Lock:
        if key not in self.locks:
            self.locks[key] = asyncio.Lock()
        return self.locks[key]

    # ------------------------------------------------------------------
    # /protect
    # ------------------------------------------------------------------
    @app_commands.command(name="protect", description="Защитить участника от мута (админ)")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def protect_command(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction):
            await deny_admin(interaction)
            return
        view = ProtectView(self, interaction.user.id)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)
        view.message = await interaction.original_response()

    async def apply_protection(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        expires_at: Optional[int],
        notify: bool,
        guards_value: str,
    ) -> None:
        guild = interaction.guild
        guards = parse_guards(guards_value)
        await self.repo.upsert_protection(
            guild.id, member.id, interaction.user.id, expires_at, notify, guards_value
        )
        await self.repo.add_action_log(
            guild.id, "mute_protect",
            f"user_id={member.id} by={interaction.user.id} expires_at={expires_at} "
            f"notify={int(notify)} guards={guards_value}",
        )
        # Отвечаем сразу: снятие заглушения и чтение журнала ниже — сетевые вызовы.
        await interaction.response.defer()

        # Если участник уже заглушён или в тайм-ауте — снимаем сразу.
        voice = member.voice
        if voice is not None and voice.channel is not None:
            await self._undo_voice(member, voice, guards)
        if GUARD_TIMEOUT in guards and member.is_timed_out():
            await self._remove_timeout(member)
        if guards & {GUARD_MOVE, GUARD_DISCONNECT}:
            await self._prime_audit_cache(guild)

        description = texts.PROTECT_DONE.format(
            user=member.mention,
            until=format_until(expires_at),
            guards=format_guards(guards),
            notify=texts.PROTECT_NOTIFY_ON if notify else texts.PROTECT_NOTIFY_OFF,
        )
        missing = self._missing_perms(guild, guards)
        if missing:
            description += texts.PROTECT_MISSING_PERMS.format(perms=", ".join(missing))
        if notify and not await self.repo.get_telegram_chat_ids(guild.id):
            description += texts.PROTECT_NO_TG_HINT
        embed = discord.Embed(
            title=texts.PROTECT_TITLE, description=description, colour=texts.EMBED_COLOR_SUCCESS
        )
        await interaction.edit_original_response(embed=embed, view=None)

    @staticmethod
    def _missing_perms(guild: discord.Guild, guards: set[str]) -> list[str]:
        perms = guild.me.guild_permissions
        needed = [
            (GUARD_MUTE, perms.mute_members, texts.PERM_MUTE),
            (GUARD_DEAF, perms.deafen_members, texts.PERM_DEAF),
            (GUARD_MOVE, perms.move_members, texts.PERM_MOVE),
            (GUARD_TIMEOUT, perms.moderate_members, texts.PERM_TIMEOUT),
        ]
        missing = [name for guard, ok, name in needed if guard in guards and not ok]
        if guards & {GUARD_MOVE, GUARD_DISCONNECT} and not perms.view_audit_log:
            missing.append(texts.PERM_AUDIT)
        return missing

    # ------------------------------------------------------------------
    # /unprotect
    # ------------------------------------------------------------------
    @app_commands.command(name="unprotect", description="Снять защиту от мута (админ)")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def unprotect_command(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction):
            await deny_admin(interaction)
            return
        guild = interaction.guild
        rows = await self.repo.list_protections(guild.id)
        if not rows:
            await respond(interaction, texts.UNPROTECT_EMPTY, title=texts.UNPROTECT_TITLE)
            return

        lines, options = [], []
        for row in rows:
            member = guild.get_member(row["user_id"])
            notify = texts.NOTIFY_ON_SHORT if row["notify"] else texts.NOTIFY_OFF_SHORT
            lines.append(texts.UNPROTECT_LINE.format(
                user=f"<@{row['user_id']}>", until=format_until(row["expires_at"]),
                guards=format_guards(parse_guards(row["guards"])), notify=notify,
            ))
            label = member.display_name if member else f"ID {row['user_id']}"
            options.append(discord.SelectOption(
                label=label[:100],
                description=(f"@{member.name}" if member else None),
                value=str(row["user_id"]),
            ))

        view = UnprotectView(self, interaction.user.id, options[:25])
        embed = discord.Embed(
            title=texts.UNPROTECT_TITLE,
            description=texts.UNPROTECT_LIST.format(lines="\n".join(lines))[:4000],
            colour=texts.EMBED_COLOR,
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

    async def remove_protections(
        self, interaction: discord.Interaction, user_ids: list[int]
    ) -> None:
        guild = interaction.guild
        removed = []
        for user_id in user_ids:
            if await self.repo.remove_protection(guild.id, user_id):
                removed.append(f"<@{user_id}>")
                await self.repo.add_action_log(
                    guild.id, "mute_unprotect", f"user_id={user_id} by={interaction.user.id}"
                )
        embed = discord.Embed(
            title=texts.UNPROTECT_TITLE,
            description=texts.UNPROTECT_DONE.format(users=", ".join(removed) or "—"),
            colour=texts.EMBED_COLOR_SUCCESS,
        )
        await interaction.response.edit_message(embed=embed, view=None)

    # ------------------------------------------------------------------
    # Голосовые события: mute/deaf, перетаскивание, отключение
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # Базовые счётчики журнала аудита, чтобы старые склеенные записи
        # не приняли за свежие перемещения.
        for guild_id in await self.repo.protected_guild_ids():
            guild = self.bot.get_guild(guild_id)
            if guild is not None:
                await self._prime_audit_cache(guild)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        moved = before.channel is not None and after.channel is not None \
            and before.channel != after.channel
        disconnected = before.channel is not None and after.channel is None
        muted_now = after.channel is not None and (after.mute or after.deaf)
        if member.bot or not (moved or disconnected or muted_now):
            return
        guild = member.guild
        if not await self.module_enabled(guild.id):
            return
        async with self._lock((guild.id, member.id)):
            protection = await self.repo.get_protection(guild.id, member.id)
            if protection is None:
                return
            guards = parse_guards(protection["guards"])

            if moved and GUARD_MOVE in guards:
                await self._handle_move(member, before.channel, after.channel, protection)
            elif disconnected and GUARD_DISCONNECT in guards:
                await self._handle_disconnect(member, before.channel, protection)

            if muted_now and guards & {GUARD_MUTE, GUARD_DEAF}:
                await self._handle_mute(member, before, after, protection, guards)

    async def _handle_mute(
        self, member: discord.Member, before, after, protection, guards: set[str]
    ) -> None:
        guild = member.guild
        # Состояние могло уже поменяться, пока ждали лок.
        voice = member.voice
        if voice is None or voice.channel is None:
            return
        muted = GUARD_MUTE in guards and after.mute and not before.mute
        deafened = GUARD_DEAF in guards and after.deaf and not before.deaf
        relevant = (GUARD_MUTE in guards and voice.mute) or (GUARD_DEAF in guards and voice.deaf)
        if not relevant:
            return
        # Server mute сохраняется между каналами: участник мог зайти
        # (или перейти) в голосовой канал уже заглушённым.
        joined_muted = not (muted or deafened)

        if joined_muted:
            action = texts.ACTION_JOINED_MUTED
        else:
            action = ", ".join(
                name for flag, name in ((muted, texts.ACTION_MUTE), (deafened, texts.ACTION_DEAF))
                if flag
            )
        if not await self._undo_voice(member, voice, guards):
            await self._notify_failed(
                member, protection, texts.ICON_MUTE, action,
                texts.UNDO_FAILED_VOICE.format(channel=tg_escape(voice.channel.name)),
            )
            return
        await self.repo.add_action_log(
            guild.id, "mute_blocked",
            f"user_id={member.id} mute={int(after.mute)} deaf={int(after.deaf)}",
        )
        if not protection["notify"]:
            return
        actor = None if joined_muted else await self._find_update_actor(
            member, lambda after_diff: (muted and getattr(after_diff, "mute", None) is True)
            or (deafened and getattr(after_diff, "deaf", None) is True)
        )
        await self._notify_attempt(
            member, protection, texts.TITLE_MUTE, texts.ICON_MUTE, action,
            actor, texts.RESULT_UNDONE,
        )

    async def _handle_move(
        self, member: discord.Member, source, target, protection
    ) -> None:
        guild = member.guild
        actor, audit_ok = await self._find_counted_actor(
            guild, member, discord.AuditLogAction.member_move, target.id
        )
        if actor is None:
            # Перешёл сам (или журнал недоступен — тогда не рискуем
            # откатывать собственный переход участника).
            return
        action = texts.ACTION_MOVE.format(source=tg_escape(source.name), target=tg_escape(target.name))
        try:
            await member.move_to(source, reason=texts.UNMUTE_REASON)
        except discord.HTTPException as exc:
            log.info("Не удалось вернуть %s в канал %s: %s", member.id, source.id, exc)
            await self._notify_failed(
                member, protection, texts.ICON_MOVE, action,
                texts.UNDO_FAILED_MOVE.format(channel=tg_escape(source.name)),
            )
            return
        await self.repo.add_action_log(
            guild.id, "move_blocked", f"user_id={member.id} by={actor.id} back_to={source.id}"
        )
        if protection["notify"]:
            await self._notify_attempt(
                member, protection, texts.TITLE_MOVE, texts.ICON_MOVE, action,
                actor, texts.RESULT_MOVED_BACK,
            )

    async def _handle_disconnect(self, member: discord.Member, channel, protection) -> None:
        guild = member.guild
        actor, _ = await self._find_counted_actor(
            guild, member, discord.AuditLogAction.member_disconnect, None
        )
        if actor is None:
            return  # вышел сам
        await self.repo.add_action_log(
            guild.id, "disconnect_seen", f"user_id={member.id} by={actor.id}"
        )
        if protection["notify"]:
            await self._notify_attempt(
                member, protection, texts.TITLE_DISCONNECT, texts.ICON_DISCONNECT,
                texts.ACTION_DISCONNECT.format(channel=tg_escape(channel.name)),
                actor, texts.RESULT_DISCONNECT,
            )

    # ------------------------------------------------------------------
    # Тайм-аут
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if after.bot or not after.is_timed_out() or before.timed_out_until == after.timed_out_until:
            return
        guild = after.guild
        key = (guild.id, after.id)
        if key in self.own_timeouts:
            self.own_timeouts.discard(key)
            return
        if not await self.module_enabled(guild.id):
            return
        async with self._lock(key):
            protection = await self.repo.get_protection(guild.id, after.id)
            if protection is None or GUARD_TIMEOUT not in parse_guards(protection["guards"]):
                return
            remaining = (after.timed_out_until - discord.utils.utcnow()).total_seconds()
            action = texts.ACTION_TIMEOUT.format(minutes=max(1, round(remaining / 60)))
            if not await self._remove_timeout(after):
                await self._notify_failed(
                    after, protection, texts.ICON_TIMEOUT, action, texts.UNDO_FAILED_TIMEOUT
                )
                return
            await self.repo.add_action_log(guild.id, "timeout_blocked", f"user_id={after.id}")
            if protection["notify"]:
                actor = await self._find_update_actor(
                    after, lambda diff: getattr(diff, "timed_out_until", None) is not None
                )
                await self._notify_attempt(
                    after, protection, texts.TITLE_TIMEOUT, texts.ICON_TIMEOUT, action,
                    actor, texts.RESULT_TIMEOUT,
                )

    # ------------------------------------------------------------------
    # Отмена действий
    # ------------------------------------------------------------------
    async def _undo_voice(
        self, member: discord.Member, voice: discord.VoiceState, guards: set[str]
    ) -> bool:
        changes = {}
        if GUARD_MUTE in guards and voice.mute:
            changes["mute"] = False
        if GUARD_DEAF in guards and voice.deaf:
            changes["deafen"] = False
        if not changes:
            return True
        try:
            await member.edit(**changes, reason=texts.UNMUTE_REASON)
        except discord.Forbidden:
            log.warning("Нет прав снять заглушение с %s на сервере %s", member.id, member.guild.id)
            return False
        except discord.HTTPException as exc:
            # Например, участник успел выйти из голосового канала.
            log.info("Не удалось снять заглушение с %s: %s", member.id, exc)
            return False
        return True

    async def _remove_timeout(self, member: discord.Member) -> bool:
        try:
            await member.timeout(None, reason=texts.UNMUTE_REASON)
        except discord.HTTPException as exc:
            log.warning("Не удалось снять тайм-аут с %s: %s", member.id, exc)
            return False
        return True

    async def timeout_actor(self, actor: discord.Member, minutes: int, reason: str) -> None:
        """Тайм-аут от имени бота (кнопка в Telegram) — защита его не снимает."""
        key = (actor.guild.id, actor.id)
        self.own_timeouts.add(key)
        try:
            await actor.timeout(dt.timedelta(minutes=minutes), reason=reason)
        except Exception:
            self.own_timeouts.discard(key)
            raise

    # ------------------------------------------------------------------
    # Журнал аудита
    # ------------------------------------------------------------------
    async def _find_update_actor(self, member: discord.Member, matches):
        """Ищет в member_update, кто изменил участника.

        Возвращает автора записи (discord.User/Member) или HTML-строку
        «неизвестно…» для уведомления.
        """
        user = None
        await asyncio.sleep(AUDIT_DELAY)
        since = discord.utils.utcnow() - AUDIT_WINDOW
        try:
            async for entry in member.guild.audit_logs(
                limit=20, action=discord.AuditLogAction.member_update
            ):
                if entry.created_at < since:
                    break
                if entry.target is None or entry.target.id != member.id:
                    continue
                if matches(entry.after):
                    user = entry.user
                    break
        except discord.Forbidden:
            return texts.ACTOR_NO_AUDIT
        except discord.HTTPException as exc:
            log.info("Не удалось прочитать журнал аудита %s: %s", member.guild.id, exc)
        return user if user is not None else texts.ACTOR_UNKNOWN

    async def _prime_audit_cache(self, guild: discord.Guild) -> None:
        if not guild.me.guild_permissions.view_audit_log:
            return
        for action in (discord.AuditLogAction.member_move, discord.AuditLogAction.member_disconnect):
            try:
                async for entry in guild.audit_logs(limit=50, action=action):
                    self._remember_count(guild.id, entry)
            except discord.HTTPException as exc:
                log.info("Не удалось прочитать журнал аудита %s: %s", guild.id, exc)
                return

    def _remember_count(self, guild_id: int, entry: discord.AuditLogEntry) -> Optional[int]:
        """Запоминает count записи и возвращает предыдущий (None — не видели)."""
        counts = self.audit_counts.setdefault(guild_id, {})
        previous = counts.get(entry.id)
        counts[entry.id] = getattr(entry.extra, "count", None) or 1
        if len(counts) > AUDIT_CACHE_LIMIT:
            # id — snowflake: меньше значит старее; старые записи больше не нужны.
            for entry_id in sorted(counts)[: len(counts) - AUDIT_CACHE_LIMIT]:
                del counts[entry_id]
        return previous

    async def _find_counted_actor(
        self,
        guild: discord.Guild,
        member: discord.Member,
        action: discord.AuditLogAction,
        channel_id: Optional[int],
    ) -> tuple[Optional[discord.abc.User], bool]:
        """Кто только что переместил/отключил участника — по приросту журнала.

        В member_move/member_disconnect нет target, а повторы одного
        модератора склеиваются в одну запись с растущим extra.count. Поэтому
        «действие только что было» = новая свежая запись или выросший count.
        Действия бота и самого участника не считаются. Возвращает
        (виновник или None, журнал доступен).
        """
        await asyncio.sleep(AUDIT_DELAY)
        now = discord.utils.utcnow()
        found = None
        try:
            # Просматриваем все записи, чтобы обновить счётчики и тех, что
            # не подошли (например, возврат участника самим ботом).
            async for entry in guild.audit_logs(limit=25, action=action):
                previous = self._remember_count(guild.id, entry)
                count = getattr(entry.extra, "count", None) or 1
                fresh = previous is None and now - entry.created_at < AUDIT_FRESH
                grew = previous is not None and count > previous
                if found is not None or not (fresh or grew) or entry.user is None:
                    continue
                if entry.user.id in (self.bot.user.id, member.id):
                    continue
                channel = getattr(entry.extra, "channel", None)
                if channel_id is not None and channel is not None and channel.id != channel_id:
                    continue
                found = entry.user
        except discord.Forbidden:
            return None, False
        except discord.HTTPException as exc:
            log.info("Не удалось прочитать журнал аудита %s: %s", guild.id, exc)
            return None, False
        return found, True

    # ------------------------------------------------------------------
    # Уведомления
    # ------------------------------------------------------------------
    async def _notify_attempt(
        self,
        member: discord.Member,
        protection,
        title: str,
        icon: str,
        action: str,
        actor,
        result: str,
    ) -> None:
        """``actor`` — участник Discord (тогда под уведомлением кнопки наказания)
        или готовая HTML-строка («неизвестно…»)."""
        guild = member.guild
        cooldown = self.config.mute_guard_notify_cooldown
        last = protection["last_notified_at"]
        if last is not None and time.time() - last < cooldown:
            await self.repo.add_suppressed_attempt(guild.id, member.id)
            return

        actor_user = actor if isinstance(actor, (discord.User, discord.Member)) else None
        text = texts.TG_GUARD_NOTICE.format(
            title=title,
            member=_member_display(member),
            icon=icon,
            action=action,
            actor=_member_display(actor_user) if actor_user else (actor or texts.ACTOR_UNKNOWN),
            result=result,
        )
        if protection["suppressed_attempts"]:
            text += texts.TG_ATTEMPT_SUPPRESSED.format(count=protection["suppressed_attempts"])
        text += texts.TG_ATTEMPT_COOLDOWN.format(minutes=cooldown // 60)

        await self.repo.mark_notified(guild.id, member.id)
        markup = attempt_keyboard(
            guild.id, member.id,
            actor_user.id if actor_user else None,
            can_extend=protection["expires_at"] is not None,
        )
        await notify_guild(self.bot.tg_bot, self.repo, guild.id, guild.name, text, markup)

    async def _notify_failed(
        self, member: discord.Member, protection, icon: str, action: str, reason: str
    ) -> None:
        if not protection["notify"]:
            return
        last = protection["last_notified_at"]
        if last is not None and time.time() - last < self.config.mute_guard_notify_cooldown:
            return
        guild = member.guild
        await self.repo.mark_notified(guild.id, member.id)
        await notify_guild(
            self.bot.tg_bot, self.repo, guild.id, guild.name,
            texts.TG_UNDO_FAILED.format(
                member=_member_display(member), icon=icon, action=action, reason=reason
            ),
        )

    # ------------------------------------------------------------------
    # Очистка истёкших защит
    # ------------------------------------------------------------------
    @tasks.loop(minutes=1)
    async def cleanup_expired(self) -> None:
        for row in await self.repo.pop_expired():
            await self.repo.add_action_log(
                row["guild_id"], "mute_protect_expired", f"user_id={row['user_id']}"
            )

    @cleanup_expired.before_loop
    async def _before_cleanup(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MuteGuardCog(bot))
