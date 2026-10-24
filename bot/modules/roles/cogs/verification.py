"""Основной флоу верификации: on_member_join, напоминания, кик, /role."""

import asyncio
import datetime as dt
import logging
import math
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.core.utils import format_hex_color
from bot.discord_bot.helpers import (
    add_roles_safe,
    remove_roles_safe,
    respond,
    with_backoff,
)
from bot.discord_bot.module_cog import ModuleCog
from bot.modules.roles import models, texts
from bot.modules.roles.notify import (
    send_join_notice,
    send_returned_notice,
    send_verification_notice,
)
from bot.modules.roles.repository import RolesRepository
from bot.modules.roles.views.modals import EditRoleModal, RoleModal
from bot.modules.roles.views.selects import OptionalRolesView

log = logging.getLogger(__name__)

Key = tuple[int, int]  # (guild_id, user_id)


def _display_order(role: discord.Role) -> tuple[int, int]:
    """Ключ порядка ролей, как в списке Discord: при равной позиции выше старшая роль."""
    return (role.position, -role.id)


def _directly_below(
    role: discord.Role, above: discord.Role, roles: list[discord.Role]
) -> bool:
    """True, если ``role`` стоит в списке сразу под ``above``."""
    low, high = _display_order(role), _display_order(above)
    return low < high and not any(low < _display_order(r) < high for r in roles)


class VerificationCog(ModuleCog):
    module_name = "roles"

    def __init__(self, bot: commands.Bot):
        super().__init__(bot)
        self.repo = RolesRepository(bot.db)
        # Активные таймеры верификации и локи от двойных нажатий — в памяти;
        # источник истины — pending_verifications в БД (восстанавливается при старте).
        self.timers: dict[Key, asyncio.Task] = {}
        self.locks: dict[Key, asyncio.Lock] = {}
        self._restored = False
        # guild_id → кликабельное упоминание </role:ID> (ID команды у Discord).
        self._role_mentions: dict[int, str] = {}

    def cog_unload(self) -> None:
        for task in self.timers.values():
            task.cancel()
        self.timers.clear()

    def _lock(self, key: Key) -> asyncio.Lock:
        if key not in self.locks:
            self.locks[key] = asyncio.Lock()
        return self.locks[key]

    # ------------------------------------------------------------------
    # Вспомогательные обёртки
    # ------------------------------------------------------------------
    def _get_text_channel(
        self, guild: discord.Guild, channel_id: Optional[int]
    ) -> Optional[discord.TextChannel]:
        if not channel_id:
            return None
        channel = guild.get_channel(channel_id)
        return channel if isinstance(channel, discord.TextChannel) else None

    async def _log_to_channel(
        self,
        guild: discord.Guild,
        title: str,
        description: str,
        colour: int = texts.EMBED_COLOR,
    ) -> None:
        settings = await self.repo.get_settings(guild.id)
        channel = self._get_text_channel(
            guild, settings["log_channel_id"] if settings else None
        )
        if channel is None:
            return
        embed = discord.Embed(title=title, description=description, colour=colour)
        await with_backoff(lambda: channel.send(embed=embed))

    async def _delete_reminder(self, guild: discord.Guild, message_id: Optional[int]):
        if not message_id:
            return
        settings = await self.repo.get_settings(guild.id)
        channel = self._get_text_channel(
            guild, settings["roles_channel_id"] if settings else None
        )
        if channel is None:
            return
        await with_backoff(
            lambda: channel.get_partial_message(message_id).delete()
        )

    async def _ensure_no_role(
        self, guild: discord.Guild, settings
    ) -> Optional[discord.Role]:
        """Возвращает роль «Не имеет ролей», восстанавливая её при необходимости.

        Если роль удалили вручную — ищем по имени, иначе создаём заново
        и обновляем сохранённый id в настройках.
        """
        no_role = (
            guild.get_role(settings["no_role_id"]) if settings["no_role_id"] else None
        )
        if no_role is None:
            no_role = discord.utils.get(guild.roles, name=texts.NO_ROLE_NAME)
        if no_role is None:
            try:
                no_role = await guild.create_role(
                    name=texts.NO_ROLE_NAME,
                    colour=discord.Colour.light_grey(),
                    reason="Служебная роль верификации (восстановлена автоматически)",
                )
                await self.repo.add_action_log(
                    guild.id, "no_role_recreated", f"role_id={no_role.id}"
                )
            except discord.Forbidden:
                return None
        if no_role.id != settings["no_role_id"]:
            await self.repo.set_no_role_id(guild.id, no_role.id)
        return no_role

    async def _role_command_mention(self, guild: discord.Guild) -> str:
        """Кликабельное упоминание /role; при неудаче — просто `/role`.

        Если команды синхронизированы с сервером (TEST_GUILD_ID), у серверной
        копии свой ID — поэтому сначала ищем среди команд сервера.
        """
        if guild.id in self._role_mentions:
            return self._role_mentions[guild.id]
        mention = None
        try:
            scopes = [guild, None] if guild.id == self.config.test_guild_id else [None]
            for scope in scopes:
                command = discord.utils.get(
                    await self.bot.tree.fetch_commands(guild=scope), name="role"
                )
                if command is not None:
                    mention = command.mention
                    break
        except discord.HTTPException as exc:
            log.info("Не удалось получить ID команды /role: %s", exc)
            return "`/role`"
        self._role_mentions[guild.id] = mention or "`/role`"
        return self._role_mentions[guild.id]

    async def _position_role_below_bot(
        self, guild: discord.Guild, role: discord.Role
    ) -> bool:
        """Поднимает роль на позицию сразу под верхней ролью бота.

        Номера позиций в Discord бывают с повторами и пропусками, а кеш ролей
        сразу после create_role может быть устаревшим, поэтому результат
        проверяем по свежему списку ролей с сервера и по фактическому порядку
        (как его показывает Discord), а не по арифметике номеров.
        """
        bot_top_id = guild.me.top_role.id
        roles: list[discord.Role] = []
        for attempt in range(4):
            try:
                roles = await guild.fetch_roles()
            except discord.HTTPException:
                await asyncio.sleep(1 + attempt)
                continue
            by_id = {r.id: r for r in roles}
            mine, bot_top = by_id.get(role.id), by_id.get(bot_top_id)
            if mine is None or bot_top is None:
                return False
            if _directly_below(mine, bot_top, roles):
                return True
            if attempt == 3:
                break
            try:
                await guild.edit_role_positions(
                    positions={role: max(1, bot_top.position - 1)},
                    reason="Именная роль — сразу под ролью бота",
                )
            except discord.Forbidden:
                return False
            except discord.HTTPException as exc:
                log.info("Не удалось переместить роль %s: %s", role.id, exc)
            await asyncio.sleep(0.5 + attempt)

        log.warning(
            "Роль %s не встала под роль бота на сервере %s; верх списка: %s",
            role.id, guild.id,
            [(r.name, r.position, r.id) for r in sorted(roles, key=_display_order, reverse=True)[:8]],
        )
        return False

    # ------------------------------------------------------------------
    # Восстановление таймеров после рестарта
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._restored:
            return
        self._restored = True
        rows = await self.repo.get_active_pendings()
        for row in rows:
            guild = self.bot.get_guild(row["guild_id"])
            if guild is None:
                continue
            member = guild.get_member(row["user_id"])
            if member is None:
                # Участник ушёл, пока бот был выключен, — чистим запись.
                await self.repo.delete_pending(row["guild_id"], row["user_id"])
                continue
            self._start_timer(row["guild_id"], row["user_id"], row["joined_at"])
        if rows:
            log.info("Восстановлено таймеров верификации: %d", len(rows))

    # ------------------------------------------------------------------
    # События входа/выхода участников
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot or not await self.module_enabled(member.guild.id):
            return
        guild = member.guild
        settings = await self.repo.get_settings(guild.id)
        if settings is None:
            log.info("Сервер %s не настроен — пропускаю on_member_join", guild.id)
            return

        # Вернувшийся участник: роли восстанавливаем, верификацию не требуем.
        if await self._restore_returning(member, settings):
            return

        no_role = await self._ensure_no_role(guild, settings)
        if no_role is not None:
            if await add_roles_safe(member, [no_role], reason="Новый участник: верификация"):
                await self._log_to_channel(
                    guild,
                    "⚠️ Не хватает прав",
                    texts.SETUP_ROLE_HIERARCHY_WARNING,
                    texts.EMBED_COLOR_WARNING,
                )
        else:
            log.warning(
                "Не удалось найти/создать роль «%s» на сервере %s — не хватает прав",
                texts.NO_ROLE_NAME,
                guild.id,
            )

        # Повторный вход до истечения кика — таймер сбрасывается заново
        # (upsert перезаписывает joined_at и статус).
        joined_at = int(time.time())
        await self.repo.upsert_pending(guild.id, member.id, joined_at)
        old_task = self.timers.pop((guild.id, member.id), None)
        if old_task:
            old_task.cancel()
        self._start_timer(guild.id, member.id, joined_at)
        await self.repo.add_action_log(guild.id, "join", f"user_id={member.id}")
        await send_join_notice(
            self.bot.tg_bot, self.repo, guild, member,
            minutes=self.config.verification_timeout // 60,
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        guild = member.guild
        if member.bot or not await self.module_enabled(guild.id):
            return
        row = await self.repo.get_pending(guild.id, member.id)
        if row is None or row["status"] not in models.ACTIVE_STATUSES:
            await self._save_role_snapshot(member)
            return
        # Участник ушёл сам или был кикнут вручную во время ожидания:
        # останавливаем таймер и чистим запись (кик ботом сюда не попадает —
        # он выставляет статус kicked до вызова kick()).
        task = self.timers.pop((guild.id, member.id), None)
        if task:
            task.cancel()
        await self._delete_reminder(guild, row["last_reminder_message_id"])
        await self.repo.delete_pending(guild.id, member.id)
        await self.repo.add_action_log(guild.id, "left_pending", f"user_id={member.id}")
        await self._log_to_channel(
            guild,
            texts.LEFT_LOG_TITLE,
            texts.LEFT_LOG_TEXT.format(user=f"{member.mention} (`{member}`)"),
            texts.EMBED_COLOR_WARNING,
        )

    # ------------------------------------------------------------------
    # Защита от ручной выдачи ролей во время верификации
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        added = [role for role in after.roles if role not in before.roles]
        if not added or after.bot:
            return
        guild = after.guild
        # Финальный шаг верификации выдаёт роли под этим локом и закрывает
        # pending до его освобождения — собственные выдачи бота не трогаем.
        if self._lock((guild.id, after.id)).locked():
            return
        if not await self.module_enabled(guild.id):
            return
        row = await self.repo.get_pending(guild.id, after.id)
        if row is None or row["status"] not in models.ACTIVE_STATUSES:
            return

        guarded = set(await self.repo.get_mandatory_roles(guild.id))
        guarded |= set(await self.repo.get_optional_roles(guild.id))
        revoked = [role for role in added if role.id in guarded]
        if not revoked:
            return

        failed = await remove_roles_safe(
            after, revoked, reason="Роли выдаются только после верификации (/role)"
        )
        removed = [role for role in revoked if role not in failed]
        if failed:
            await self._log_to_channel(
                guild,
                "⚠️ Не хватает прав",
                texts.LOG_MANUAL_REVOKE_FAILED.format(
                    user=f"{after.mention} (`{after}`)",
                    roles=", ".join(role.mention for role in failed),
                ),
                texts.EMBED_COLOR_WARNING,
            )
        if not removed:
            return

        actor = await self._find_role_actor(guild, after, removed)
        if actor is not None and actor.id == self.bot.user.id:
            return
        await self.repo.add_action_log(
            guild.id, "manual_roles_revoked",
            f"user_id={after.id} by={actor.id if actor else None} "
            f"roles={','.join(str(role.id) for role in removed)}",
        )
        await self._warn_manual_roles(guild, after, actor, removed, row["joined_at"])

    async def _find_role_actor(
        self, guild: discord.Guild, member: discord.Member, roles: list[discord.Role]
    ) -> Optional[discord.abc.User]:
        """Ищет в журнале аудита, кто выдал участнику роли (None — не удалось)."""
        await asyncio.sleep(1.5)  # запись в журнале появляется чуть позже события
        role_ids = {role.id for role in roles}
        since = discord.utils.utcnow() - dt.timedelta(minutes=2)
        try:
            async for entry in guild.audit_logs(
                limit=20, action=discord.AuditLogAction.member_role_update
            ):
                if entry.created_at < since:
                    break
                if entry.target is None or entry.target.id != member.id:
                    continue
                if role_ids & {role.id for role in getattr(entry.after, "roles", [])}:
                    return entry.user
        except discord.HTTPException as exc:
            log.info("Не удалось прочитать журнал аудита %s: %s", guild.id, exc)
        return None

    async def _warn_manual_roles(
        self,
        guild: discord.Guild,
        member: discord.Member,
        actor: Optional[discord.abc.User],
        roles: list[discord.Role],
        joined_at: int,
    ) -> None:
        minutes_left = max(
            1, math.ceil((joined_at + self.config.verification_timeout - time.time()) / 60)
        )
        role_list = ", ".join(role.mention for role in roles)
        template = texts.MANUAL_ROLES_TEXT if actor else texts.MANUAL_ROLES_TEXT_UNKNOWN
        embed = discord.Embed(
            title=texts.MANUAL_ROLES_TITLE,
            description=template.format(
                admin=actor.mention if actor else "",
                member=member.mention,
                roles=role_list,
                command=await self._role_command_mention(guild),
                minutes=minutes_left,
            ),
            colour=texts.EMBED_COLOR_WARNING,
        )
        settings = await self.repo.get_settings(guild.id)
        pings_channel = self._get_text_channel(
            guild, settings["pings_channel_id"] if settings else None
        )
        if pings_channel is not None:
            # Пингуем только администратора — участника в этом сообщении не зовём.
            await with_backoff(lambda: pings_channel.send(
                content=actor.mention if actor else None,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    users=[actor] if actor else False, roles=False, everyone=False
                ),
            ))
        await self._log_to_channel(
            guild,
            texts.LOG_MANUAL_ROLES_TITLE,
            texts.LOG_MANUAL_ROLES_TEXT.format(
                admin=f"{actor.mention} (`{actor}`)" if actor else texts.ACTOR_UNKNOWN,
                user=f"{member.mention} (`{member}`)",
                roles=role_list,
            ),
            texts.EMBED_COLOR_WARNING,
        )

    # ------------------------------------------------------------------
    # Возврат ролей при перезаходе
    # ------------------------------------------------------------------
    async def _removed_by_moderator(self, guild: discord.Guild, member: discord.Member) -> bool:
        """Участника кикнули или забанили (а не ушёл сам) — по журналу аудита."""
        await asyncio.sleep(1.5)  # запись в журнале появляется чуть позже события
        since = discord.utils.utcnow() - dt.timedelta(minutes=1)
        try:
            async for entry in guild.audit_logs(limit=10):
                if entry.created_at < since:
                    break
                if entry.action not in (discord.AuditLogAction.kick, discord.AuditLogAction.ban):
                    continue
                if entry.target is not None and entry.target.id == member.id:
                    return True
        except discord.HTTPException as exc:
            log.info("Не удалось прочитать журнал аудита %s: %s", guild.id, exc)
        return False

    async def _save_role_snapshot(self, member: discord.Member) -> None:
        """Запоминает роли ушедшего участника, чтобы вернуть их при перезаходе.

        Тем, кого кикнули или забанили, роли не сохраняем — их возвращение
        должно проходить через обычную верификацию.
        """
        guild = member.guild
        settings = await self.repo.get_settings(guild.id)
        no_role_id = settings["no_role_id"] if settings else None
        role_ids = [
            role.id for role in member.roles
            if not role.is_default() and not role.managed and role.id != no_role_id
        ]
        if not role_ids:
            return
        if await self._removed_by_moderator(guild, member):
            log.info("Участник %s удалён модератором — роли не сохраняю", member.id)
            return
        await self.repo.save_role_snapshot(guild.id, member.id, role_ids)
        await self.repo.add_action_log(
            guild.id, "roles_saved", f"user_id={member.id} count={len(role_ids)}"
        )

    async def _restore_returning(self, member: discord.Member, settings) -> bool:
        """Выдаёт вернувшемуся участнику сохранённые роли. True — верификация не нужна."""
        guild = member.guild
        role_ids = await self.repo.pop_role_snapshot(guild.id, member.id)
        if not role_ids:
            return False
        top = guild.me.top_role
        roles = [
            role for rid in role_ids
            if (role := guild.get_role(rid)) is not None
            and not role.managed and role < top and role.id != settings["no_role_id"]
        ]
        failed = await add_roles_safe(member, roles, reason="Участник вернулся: роли восстановлены")
        restored = [role for role in roles if role not in failed]
        if not restored:
            return False

        await self.repo.add_action_log(
            guild.id, "returned", f"user_id={member.id} roles={len(restored)}"
        )
        description = texts.LOG_RETURNED_TEXT.format(
            user=f"{member.mention} (`{member}`)",
            roles=", ".join(role.mention for role in restored),
        )
        if failed:
            description += texts.LOG_RETURNED_FAILED.format(
                roles=", ".join(role.mention for role in failed)
            )
        await self._log_to_channel(
            guild, texts.LOG_RETURNED_TITLE, description, texts.EMBED_COLOR_SUCCESS
        )
        await send_returned_notice(self.bot.tg_bot, self.repo, guild, member, restored)
        return True

    # ------------------------------------------------------------------
    # Действия администратора из Telegram
    # ------------------------------------------------------------------
    async def _close_pending(self, guild: discord.Guild, user_id: int, row, status: str) -> None:
        task = self.timers.pop((guild.id, user_id), None)
        if task:
            task.cancel()
        await self._delete_reminder(guild, row["last_reminder_message_id"])
        await self.repo.set_pending_status(guild.id, user_id, status)
        await self.repo.set_reminder_task_active(guild.id, user_id, False)

    async def extend_verification(
        self, guild: discord.Guild, user_id: int, seconds: int
    ) -> Optional[int]:
        """Продлевает верификацию. Возвращает минуты до кика или None, если её уже нет."""
        key = (guild.id, user_id)
        if self._lock(key).locked():
            return None
        row = await self.repo.get_pending(guild.id, user_id)
        if row is None or row["status"] not in models.ACTIVE_STATUSES:
            return None
        joined_at = row["joined_at"] + seconds
        await self.repo.set_pending_joined_at(guild.id, user_id, joined_at)
        old_task = self.timers.pop(key, None)
        if old_task:
            old_task.cancel()
        self._start_timer(guild.id, user_id, joined_at)
        await self.repo.add_action_log(guild.id, "verification_extended", f"user_id={user_id}")
        return max(1, math.ceil((joined_at + self.config.verification_timeout - time.time()) / 60))

    async def skip_verification(
        self, guild: discord.Guild, member: discord.Member, admin: discord.Member
    ) -> bool:
        """Завершает верификацию без именной роли: обязательные роли, без «Не имеет ролей».

        Именную роль участник сможет оформить позже через /role.
        """
        async with self._lock((guild.id, member.id)):
            row = await self.repo.get_pending(guild.id, member.id)
            if row is None or row["status"] not in models.ACTIVE_STATUSES:
                return False
            settings = await self.repo.get_settings(guild.id)
            mandatory = [
                role for rid in await self.repo.get_mandatory_roles(guild.id)
                if (role := guild.get_role(rid)) is not None
            ]
            reason = f"Верификация пропущена администратором {admin}"
            failed = await add_roles_safe(member, mandatory, reason=reason)
            no_role = guild.get_role(settings["no_role_id"]) if settings and settings["no_role_id"] else None
            if no_role is not None:
                failed += await remove_roles_safe(member, [no_role], reason=reason)
            await self._close_pending(guild, member.id, row, models.STATUS_COMPLETED)
            await self.repo.add_action_log(
                guild.id, "verification_skipped", f"user_id={member.id} by={admin.id}"
            )
            description = texts.LOG_SKIPPED_TEXT.format(
                admin=f"{admin.mention} (`{admin}`)", user=f"{member.mention} (`{member}`)"
            )
            if failed:
                description += texts.LOG_RETURNED_FAILED.format(
                    roles=", ".join(role.mention for role in failed)
                )
            await self._log_to_channel(
                guild, texts.LOG_SKIPPED_TITLE, description, texts.EMBED_COLOR_SUCCESS
            )
        return True

    async def kick_pending(
        self, guild: discord.Guild, member: discord.Member, admin: discord.Member
    ) -> bool:
        """Кикает участника, ещё проходящего верификацию. Forbidden пробрасывается."""
        row = await self.repo.get_pending(guild.id, member.id)
        if row is None or row["status"] not in models.ACTIVE_STATUSES:
            return False
        # Статус — до кика, чтобы on_member_remove не принял его за уход.
        await self._close_pending(guild, member.id, row, models.STATUS_KICKED)
        await member.kick(reason=texts.KICK_BY_ADMIN_REASON.format(admin=admin))
        await self.repo.add_action_log(guild.id, "kick_by_admin", f"user_id={member.id} by={admin.id}")
        await self._log_to_channel(
            guild,
            texts.KICK_LOG_TITLE,
            texts.KICK_BY_ADMIN_LOG.format(
                admin=f"{admin.mention} (`{admin}`)", user=f"`{member}` (ID {member.id})"
            ),
            texts.EMBED_COLOR_WARNING,
        )
        return True

    # ------------------------------------------------------------------
    # Таймер напоминаний и кика
    # ------------------------------------------------------------------
    def _start_timer(self, guild_id: int, user_id: int, joined_at: int) -> None:
        key = (guild_id, user_id)
        self.timers[key] = asyncio.create_task(
            self._verification_timer(guild_id, user_id, joined_at),
            name=f"verify-{guild_id}-{user_id}",
        )

    async def _verification_timer(
        self, guild_id: int, user_id: int, joined_at: int
    ) -> None:
        key = (guild_id, user_id)
        deadline = joined_at + self.config.verification_timeout
        try:
            while True:
                row = await self.repo.get_pending(guild_id, user_id)
                if row is None or row["status"] not in models.ACTIVE_STATUSES:
                    return
                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    return
                member = guild.get_member(user_id)
                if member is None:
                    await self.repo.delete_pending(guild_id, user_id)
                    return

                remaining = deadline - time.time()
                if remaining <= 0:
                    await self._kick_for_timeout(guild, member, row)
                    return

                await self._send_reminder(guild, member, row, remaining)
                await asyncio.sleep(min(self.config.reminder_interval, remaining))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Таймер верификации %s упал", key)
        finally:
            if self.timers.get(key) is asyncio.current_task():
                self.timers.pop(key, None)

    async def _send_reminder(
        self,
        guild: discord.Guild,
        member: discord.Member,
        row,
        remaining: float,
    ) -> None:
        settings = await self.repo.get_settings(guild.id)
        channel = self._get_text_channel(
            guild, settings["roles_channel_id"] if settings else None
        )
        if channel is None:
            return

        await self._delete_reminder(guild, row["last_reminder_message_id"])

        first = row["last_reminder_message_id"] is None
        minutes_left = max(1, round(remaining / 60))
        command = await self._role_command_mention(guild)
        if first:
            embed = discord.Embed(
                title=texts.JOIN_TITLE,
                description=texts.JOIN_TEXT.format(
                    mention=member.mention,
                    command=command,
                    minutes=self.config.verification_timeout // 60,
                ),
                colour=texts.EMBED_COLOR,
            )
        else:
            embed = discord.Embed(
                title=texts.REMINDER_TITLE,
                description=texts.REMINDER_TEXT.format(
                    mention=member.mention, command=command, minutes_left=minutes_left
                ),
                colour=texts.EMBED_COLOR,
            )
        content = texts.JOIN_CALL.format(mention=member.mention, command=command)
        message = await with_backoff(
            lambda: channel.send(content=content, embed=embed)
        )
        if message is not None:
            await self.repo.set_last_reminder(guild.id, member.id, message.id)

    async def _kick_for_timeout(
        self, guild: discord.Guild, member: discord.Member, row
    ) -> None:
        # Статус выставляем ДО кика, чтобы on_member_remove не принял его
        # за добровольный выход.
        await self.repo.set_pending_status(guild.id, member.id, models.STATUS_KICKED)
        await self.repo.set_reminder_task_active(guild.id, member.id, False)
        self.timers.pop((guild.id, member.id), None)

        await self._delete_reminder(guild, row["last_reminder_message_id"])

        try:
            await member.send(texts.KICK_DM.format(guild=guild.name))
        except discord.HTTPException:
            pass  # закрытые ЛС — не критично

        try:
            await member.kick(reason=texts.KICK_REASON)
        except discord.Forbidden:
            log.warning("Нет права Kick Members на сервере %s", guild.id)
            await self._log_to_channel(
                guild,
                "⚠️ Не хватает прав",
                "Не удалось кикнуть участника: отсутствует право «Кикать участников».",
                texts.EMBED_COLOR_ERROR,
            )
            return

        await self.repo.add_action_log(guild.id, "kick", f"user_id={member.id}")
        await self._log_to_channel(
            guild,
            texts.KICK_LOG_TITLE,
            texts.KICK_LOG_TEXT.format(user=f"`{member}` (ID {member.id})"),
            texts.EMBED_COLOR_WARNING,
        )

    # ------------------------------------------------------------------
    # /role
    # ------------------------------------------------------------------
    @app_commands.command(
        name="role",
        description="Оформить именную роль (или изменить существующую)",
    )
    @app_commands.guild_only()
    async def role_command(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        settings = await self.repo.get_settings(guild.id)
        if settings is None:
            await interaction.response.send_message(
                embed=discord.Embed(
                    description=texts.NOT_CONFIGURED,
                    colour=texts.EMBED_COLOR_WARNING,
                ),
                ephemeral=True,
            )
            return

        # Режим (новый участник / редактирование) определяем строго по
        # активной записи pending_verifications, а не только по verification_log.
        pending = await self.repo.get_pending(guild.id, member.id)
        if pending is not None and pending["status"] in models.ACTIVE_STATUSES:
            await self.repo.set_pending_status(
                guild.id, member.id, models.STATUS_IN_MODAL
            )
            await interaction.response.send_modal(RoleModal(self, tracked=True))
            return

        role_id = await self.repo.get_personal_role_id(guild.id, member.id)
        if role_id is not None:
            role = guild.get_role(role_id)
            if role is None:
                await interaction.response.send_message(
                    embed=discord.Embed(
                        description=texts.EDIT_ROLE_DELETED,
                        colour=texts.EMBED_COLOR_WARNING,
                    ),
                    ephemeral=True,
                )
                return
            await interaction.response.send_modal(EditRoleModal(self, role))
            return

        # Ни pending, ни именной роли: старый участник, зашедший до появления
        # бота (если роль у него уже есть — админ может привязать её через
        # /import-roles). Запускаем обычный флоу без таймера кика.
        await interaction.response.send_modal(RoleModal(self, tracked=False))

    # ------------------------------------------------------------------
    # Обработка сабмита модалки первичной верификации
    # ------------------------------------------------------------------
    async def handle_role_modal_submit(
        self,
        interaction: discord.Interaction,
        role_name: str,
        color_value: int,
        inviter: Optional[str],
        tracked: bool,
    ) -> None:
        # Финальный шаг занимает больше 3 секунд (создание роли, выдача,
        # Telegram) — отвечаем deferred сразу, иначе interaction протухает
        # (404 Unknown interaction), дальше работаем через followup.
        await interaction.response.defer(ephemeral=True, thinking=True)

        guild = interaction.guild
        optional_ids = await self.repo.get_optional_roles(guild.id)
        optional_roles = [
            role for rid in optional_ids if (role := guild.get_role(rid)) is not None
        ]

        # Опциональные роли не настроены (или все удалены с сервера) —
        # сразу выполняем финальный шаг без select-меню.
        if not optional_roles:
            await self.finalize_verification(
                interaction,
                role_name=role_name,
                color_value=color_value,
                inviter=inviter,
                optional_role_ids=[],
                tracked=tracked,
            )
            return

        view = OptionalRolesView(
            self, optional_roles, role_name, color_value, inviter, tracked
        )
        embed = discord.Embed(
            title=texts.OPTIONAL_ROLES_TITLE,
            description=texts.OPTIONAL_ROLES_PROMPT,
            colour=texts.EMBED_COLOR,
        )
        view.message = await interaction.followup.send(
            embed=embed, view=view, ephemeral=True
        )

    # ------------------------------------------------------------------
    # Финальный шаг: создать роль, выдать всё, поздравить, уведомить
    # ------------------------------------------------------------------
    async def finalize_verification(
        self,
        interaction: discord.Interaction,
        role_name: str,
        color_value: int,
        inviter: Optional[str],
        optional_role_ids: list[int],
        tracked: bool,
    ) -> None:
        guild = interaction.guild
        member = guild.get_member(interaction.user.id)
        if member is None:
            return
        key = (guild.id, member.id)
        lock = self._lock(key)

        if lock.locked():
            await respond(interaction, texts.ALREADY_PROCESSING, ephemeral=True)
            return

        async with lock:
            # Защита от двойного submit: если верификация уже завершена —
            # молча подтверждаем и выходим.
            if tracked:
                row = await self.repo.get_pending(guild.id, member.id)
                if row is None or row["status"] == models.STATUS_COMPLETED:
                    await respond(
                        interaction, texts.ALREADY_PROCESSING, ephemeral=True
                    )
                    return

            if len(guild.roles) >= self.config.role_limit_threshold:
                await respond(
                    interaction, texts.ROLE_LIMIT_REACHED, ephemeral=True,
                    colour=texts.EMBED_COLOR_ERROR,
                )
                await self._log_to_channel(
                    guild,
                    "⚠️ Лимит ролей",
                    f"Осталось мало свободных ролей: {len(guild.roles)}/250.",
                    texts.EMBED_COLOR_WARNING,
                )
                return

            # TODO: Discord допускает одинаковые названия ролей — дубликаты
            # разрешаем сознательно; при желании добавьте суффикс здесь.
            try:
                custom_role = await guild.create_role(
                    name=role_name,
                    colour=discord.Colour(color_value),
                    reason=f"Именная роль для {member} (/role)",
                )
            except discord.Forbidden:
                await respond(
                    interaction, texts.ROLE_CREATE_FORBIDDEN, ephemeral=True,
                    colour=texts.EMBED_COLOR_ERROR,
                )
                return

            # Поднимаем роль на позицию сразу под верхней ролью бота
            # (с повторами и проверкой результата — см. хелпер).
            if not await self._position_role_below_bot(guild, custom_role):
                await self._log_to_channel(
                    guild,
                    "⚠️ Иерархия ролей",
                    texts.LOG_HIERARCHY_WARNING.format(role_name=role_name),
                    texts.EMBED_COLOR_WARNING,
                )

            settings = await self.repo.get_settings(guild.id)
            mandatory_ids = await self.repo.get_mandatory_roles(guild.id)
            to_add = [custom_role]
            for rid in mandatory_ids + optional_role_ids:
                role = guild.get_role(rid)
                if role is not None and role not in to_add:
                    to_add.append(role)

            # Роли выдаём по одной: уже имеющиеся (например, выданные вручную)
            # пропускаются, а сбой одной роли не срывает всю верификацию.
            failed = await add_roles_safe(member, to_add, reason="Верификация /role")
            if custom_role in failed:
                await respond(
                    interaction, texts.ROLE_CREATE_FORBIDDEN, ephemeral=True,
                    colour=texts.EMBED_COLOR_ERROR,
                )
                await self._log_to_channel(
                    guild,
                    "⚠️ Не хватает прав",
                    texts.SETUP_ROLE_HIERARCHY_WARNING,
                    texts.EMBED_COLOR_ERROR,
                )
                return
            no_role = (
                guild.get_role(settings["no_role_id"])
                if settings and settings["no_role_id"]
                else None
            )
            if no_role is not None:
                failed += await remove_roles_safe(
                    member, [no_role], reason="Верификация пройдена"
                )
            if failed:
                await self._log_to_channel(
                    guild,
                    "⚠️ Не хватает прав",
                    texts.LOG_ROLES_PARTIAL.format(
                        user=f"{member.mention} (`{member}`)",
                        roles=", ".join(role.mention for role in failed),
                    ),
                    texts.EMBED_COLOR_WARNING,
                )

            # Останавливаем таймер и закрываем pending-запись.
            if tracked:
                task = self.timers.pop(key, None)
                if task:
                    task.cancel()
                row = await self.repo.get_pending(guild.id, member.id)
                if row is not None:
                    await self._delete_reminder(guild, row["last_reminder_message_id"])
                await self.repo.set_pending_status(
                    guild.id, member.id, models.STATUS_COMPLETED
                )
                await self.repo.set_reminder_task_active(guild.id, member.id, False)

            await self.repo.add_verification(guild.id, member.id, custom_role.id, inviter)
            await self.repo.bind_personal_roles(
                guild.id, [(member.id, custom_role.id)], models.SOURCE_VERIFICATION
            )
            await self.repo.add_action_log(
                guild.id, "verified", f"user_id={member.id} role_id={custom_role.id}"
            )

            # Карточка-поздравление в канал [пинги].
            pings_channel = self._get_text_channel(
                guild, settings["pings_channel_id"] if settings else None
            )
            if pings_channel is not None:
                embed = discord.Embed(
                    title=texts.SUCCESS_TITLE,
                    description=texts.SUCCESS_PING.format(
                        mention=member.mention, role_name=custom_role.name
                    ),
                    colour=discord.Colour(color_value),
                )
                await with_backoff(lambda: pings_channel.send(embed=embed))

            # Журнал.
            await self._log_to_channel(
                guild,
                texts.LOG_VERIFIED_TITLE,
                texts.LOG_VERIFIED_TEXT.format(
                    user=f"{member.mention} (`{member}`)",
                    role_name=custom_role.name,
                    color=format_hex_color(color_value),
                    inviter=inviter or texts.INVITER_NOT_SPECIFIED,
                ),
                texts.EMBED_COLOR_SUCCESS,
            )

            # Telegram-уведомление ответственным.
            await send_verification_notice(
                tg_bot=self.bot.tg_bot,
                repo=self.repo,
                guild_id=guild.id,
                guild_name=guild.name,
                member=member,
                role_name=custom_role.name,
                inviter=inviter,
            )

            await respond(
                interaction,
                texts.SUCCESS_EPHEMERAL.format(role_name=custom_role.name),
                ephemeral=True,
                title=texts.SUCCESS_TITLE,
                colour=texts.EMBED_COLOR_SUCCESS,
            )

    # ------------------------------------------------------------------
    # Редактирование существующей именной роли
    # ------------------------------------------------------------------
    async def handle_role_edit_submit(
        self,
        interaction: discord.Interaction,
        role_id: int,
        new_name: str,
        new_color: int,
    ) -> None:
        # Как и при верификации: отвечаем deferred сразу, чтобы не протух
        # 3-секундный лимит ответа на interaction.
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        member = interaction.user
        key = (guild.id, member.id)

        async with self._lock(key):
            role = guild.get_role(role_id)
            if role is None:
                await respond(
                    interaction, texts.EDIT_ROLE_DELETED, ephemeral=True,
                    colour=texts.EMBED_COLOR_WARNING,
                )
                return

            old_name = role.name
            old_color = format_hex_color(role.colour.value)
            try:
                await role.edit(
                    name=new_name,
                    colour=discord.Colour(new_color),
                    reason=f"Редактирование именной роли {member} (/role)",
                )
            except discord.Forbidden:
                await respond(
                    interaction, texts.ROLE_CREATE_FORBIDDEN, ephemeral=True,
                    colour=texts.EMBED_COLOR_ERROR,
                )
                return

            new_color_hex = format_hex_color(new_color)
            await self.repo.add_role_edit(
                guild.id, member.id, old_name, old_color, new_name, new_color_hex
            )
            await self.repo.add_action_log(
                guild.id, "role_edit", f"user_id={member.id} role_id={role_id}"
            )
            await self._log_to_channel(
                guild,
                texts.LOG_ROLE_EDITED_TITLE,
                texts.LOG_ROLE_EDITED_TEXT.format(
                    user=f"{member.mention} (`{member}`)",
                    old_name=old_name,
                    old_color=old_color,
                    new_name=new_name,
                    new_color=new_color_hex,
                ),
            )

            # Пинги/Telegram при простом редактировании — только если включён
            # флаг notify_on_role_edit (по умолчанию выключен).
            settings = await self.repo.get_settings(guild.id)
            if settings and settings["notify_on_role_edit"]:
                await send_verification_notice(
                    tg_bot=self.bot.tg_bot,
                    repo=self.repo,
                    guild_id=guild.id,
                    guild_name=guild.name,
                    member=member,
                    role_name=new_name,
                    inviter=None,
                    edited=True,
                )

            await respond(
                interaction,
                texts.EDIT_SUCCESS.format(new_name=new_name, new_color=new_color_hex),
                ephemeral=True,
                colour=texts.EMBED_COLOR_SUCCESS,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VerificationCog(bot))
