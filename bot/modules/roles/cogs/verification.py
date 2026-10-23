"""Основной флоу верификации: on_member_join, напоминания, кик, /role."""

import asyncio
import logging
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.core.utils import format_hex_color
from bot.discord_bot.helpers import respond, with_backoff
from bot.modules.roles import models, texts
from bot.modules.roles.notify import send_verification_notice
from bot.modules.roles.repository import RolesRepository
from bot.modules.roles.views.modals import EditRoleModal, RoleModal
from bot.modules.roles.views.selects import OptionalRolesView

log = logging.getLogger(__name__)

Key = tuple[int, int]  # (guild_id, user_id)


class VerificationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.repo = RolesRepository(bot.db)
        self.config = bot.config
        # Активные таймеры верификации и локи от двойных нажатий — в памяти;
        # источник истины — pending_verifications в БД (восстанавливается при старте).
        self.timers: dict[Key, asyncio.Task] = {}
        self.locks: dict[Key, asyncio.Lock] = {}
        self._restored = False

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

    async def _position_role_below_bot(
        self, guild: discord.Guild, role: discord.Role
    ) -> bool:
        """Поднимает роль на позицию сразу под верхней ролью бота.

        Сразу после create_role позиция иногда «не доезжает» из-за гонки
        с обновлением списка ролей на стороне Discord, поэтому после каждой
        попытки проверяем результат по кешу и при необходимости повторяем.
        """
        for attempt in range(3):
            top = guild.me.top_role
            target = max(1, top.position - 1)
            try:
                await guild.edit_role_positions(
                    positions={role: target},
                    reason="Именная роль — сразу под ролью бота",
                )
            except discord.Forbidden:
                return False
            except discord.HTTPException:
                await asyncio.sleep(1 + attempt)
                continue

            await asyncio.sleep(0.5)  # ждём, пока gateway обновит кеш ролей
            fresh = guild.get_role(role.id)
            if fresh is not None and fresh.position >= guild.me.top_role.position - 1:
                return True
            await asyncio.sleep(1 + attempt)
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
        if member.bot:
            return
        guild = member.guild
        settings = await self.repo.get_settings(guild.id)
        if settings is None:
            log.info("Сервер %s не настроен — пропускаю on_member_join", guild.id)
            return

        no_role = await self._ensure_no_role(guild, settings)
        if no_role is not None:
            try:
                await member.add_roles(no_role, reason="Новый участник: верификация")
            except discord.Forbidden:
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

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        guild = member.guild
        row = await self.repo.get_pending(guild.id, member.id)
        if row is None or row["status"] not in models.ACTIVE_STATUSES:
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
        if first:
            embed = discord.Embed(
                title=texts.JOIN_TITLE,
                description=texts.JOIN_TEXT.format(
                    mention=member.mention,
                    minutes=self.config.verification_timeout // 60,
                ),
                colour=texts.EMBED_COLOR,
            )
        else:
            embed = discord.Embed(
                title=texts.REMINDER_TITLE,
                description=texts.REMINDER_TEXT.format(
                    mention=member.mention, minutes_left=minutes_left
                ),
                colour=texts.EMBED_COLOR,
            )
        message = await with_backoff(
            lambda: channel.send(content=member.mention, embed=embed)
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

        last = await self.repo.get_last_verification(guild.id, member.id)
        if last is not None:
            role = guild.get_role(last["custom_role_id"])
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

        # Ни pending, ни истории: старый участник, зашедший до появления бота.
        # Запускаем обычный флоу, но без таймера кика (tracked=False).
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

            try:
                await member.add_roles(*to_add, reason="Верификация /role")
                no_role = (
                    guild.get_role(settings["no_role_id"])
                    if settings and settings["no_role_id"]
                    else None
                )
                if no_role is not None and no_role in member.roles:
                    await member.remove_roles(no_role, reason="Верификация пройдена")
            except discord.Forbidden:
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
                member_display=f"{member.display_name} ({member})",
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
                    member_display=f"{member.display_name} ({member})",
                    role_name=new_name,
                    inviter=None,
                )

            await respond(
                interaction,
                texts.EDIT_SUCCESS.format(new_name=new_name, new_color=new_color_hex),
                ephemeral=True,
                colour=texts.EMBED_COLOR_SUCCESS,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VerificationCog(bot))
