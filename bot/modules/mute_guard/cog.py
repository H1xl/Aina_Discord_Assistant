"""Защита от мута: /protect, /unprotect и автоматическое снятие заглушения.

Если защищённому участнику на сервере отключают микрофон (server mute)
или звук (server deafen), бот сразу возвращает их обратно и, если включены
уведомления, сообщает в привязанные Telegram-чаты — с указанием, кто это
сделал (по журналу аудита), не чаще раза в MUTE_GUARD_NOTIFY_COOLDOWN.
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
from bot.modules.mute_guard import texts
from bot.modules.mute_guard.repository import MuteGuardRepository
from bot.modules.mute_guard.views import ProtectView, UnprotectView, format_until
from bot.telegram_bot.notifier import notify_guild, tg_escape, tg_user

log = logging.getLogger(__name__)

# Насколько свежей должна быть запись журнала аудита, чтобы считать её
# причиной заглушения. Discord склеивает повторные одинаковые действия
# одного модератора в одну запись, поэтому окно берём с запасом.
AUDIT_WINDOW = dt.timedelta(minutes=10)
# Запись в журнале аудита появляется чуть позже события голосового канала.
AUDIT_DELAY = 1.5

Key = tuple[int, int]  # (guild_id, user_id)


def _member_display(member: discord.abc.User) -> str:
    """Участник для Telegram-уведомления (HTML)."""
    return tg_user(member.display_name, member.name)


class MuteGuardCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = bot.config
        self.repo = MuteGuardRepository(bot.db)
        # Сериализуем обработку событий одного участника: при «войне»
        # мута события сыплются пачкой.
        self.locks: dict[Key, asyncio.Lock] = {}
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
    ) -> None:
        guild = interaction.guild
        await self.repo.upsert_protection(
            guild.id, member.id, interaction.user.id, expires_at, notify
        )
        await self.repo.add_action_log(
            guild.id, "mute_protect",
            f"user_id={member.id} by={interaction.user.id} expires_at={expires_at} notify={int(notify)}",
        )

        # Если участник уже заглушён — снимаем сразу.
        voice = member.voice
        if voice is not None and voice.channel is not None and (voice.mute or voice.deaf):
            await self._undo(member, voice)

        description = texts.PROTECT_DONE.format(
            user=member.mention,
            until=format_until(expires_at),
            notify=texts.PROTECT_NOTIFY_ON if notify else texts.PROTECT_NOTIFY_OFF,
        )
        perms = guild.me.guild_permissions
        if not (perms.mute_members and perms.deafen_members):
            description += texts.PROTECT_MISSING_PERMS
        if notify and not await self.repo.get_telegram_chat_ids(guild.id):
            description += texts.PROTECT_NO_TG_HINT
        embed = discord.Embed(
            title=texts.PROTECT_TITLE, description=description, colour=texts.EMBED_COLOR_SUCCESS
        )
        await interaction.response.edit_message(embed=embed, view=None)

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
                user=f"<@{row['user_id']}>", until=format_until(row["expires_at"]), notify=notify,
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
    # Срабатывание защиты
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        if after.channel is None or not (after.mute or after.deaf):
            return
        guild = member.guild
        async with self._lock((guild.id, member.id)):
            protection = await self.repo.get_protection(guild.id, member.id)
            if protection is None:
                return
            # Состояние могло уже поменяться, пока ждали лок.
            voice = member.voice
            if voice is None or voice.channel is None or not (voice.mute or voice.deaf):
                return

            muted = after.mute and not before.mute
            deafened = after.deaf and not before.deaf
            # Server mute сохраняется между каналами: участник мог зайти
            # (или перейти) в голосовой канал уже заглушённым.
            joined_muted = not (muted or deafened)

            if not await self._undo(member, voice):
                await self._notify_undo_failed(guild, member, voice.channel, protection)
                return
            await self.repo.add_action_log(
                guild.id, "mute_blocked",
                f"user_id={member.id} mute={int(after.mute)} deaf={int(after.deaf)}",
            )
            if protection["notify"]:
                actor = None if joined_muted else await self._find_actor(
                    guild, member, mute=muted, deaf=deafened
                )
                await self._notify_attempt(
                    guild, member, protection, muted, deafened, joined_muted, actor
                )

    async def _undo(self, member: discord.Member, voice: discord.VoiceState) -> bool:
        changes = {}
        if voice.mute:
            changes["mute"] = False
        if voice.deaf:
            changes["deafen"] = False
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

    async def _find_actor(
        self, guild: discord.Guild, member: discord.Member, mute: bool, deaf: bool
    ) -> str:
        """Ищет в журнале аудита, кто заглушил участника."""
        await asyncio.sleep(AUDIT_DELAY)
        since = discord.utils.utcnow() - AUDIT_WINDOW
        try:
            async for entry in guild.audit_logs(
                limit=20, action=discord.AuditLogAction.member_update
            ):
                if entry.created_at < since:
                    break
                if entry.target is None or entry.target.id != member.id:
                    continue
                if (mute and getattr(entry.after, "mute", None) is True) or (
                    deaf and getattr(entry.after, "deaf", None) is True
                ):
                    return _member_display(entry.user) if entry.user else texts.ACTOR_UNKNOWN
        except discord.Forbidden:
            return texts.ACTOR_NO_AUDIT
        except discord.HTTPException as exc:
            log.info("Не удалось прочитать журнал аудита %s: %s", guild.id, exc)
        return texts.ACTOR_UNKNOWN

    async def _notify_attempt(
        self,
        guild: discord.Guild,
        member: discord.Member,
        protection,
        muted: bool,
        deafened: bool,
        joined_muted: bool,
        actor: Optional[str],
    ) -> None:
        cooldown = self.config.mute_guard_notify_cooldown
        last = protection["last_notified_at"]
        if last is not None and time.time() - last < cooldown:
            await self.repo.add_suppressed_attempt(guild.id, member.id)
            return

        if joined_muted:
            action = texts.ACTION_JOINED_MUTED
        else:
            action = ", ".join(
                name for flag, name in ((muted, texts.ACTION_MUTE), (deafened, texts.ACTION_DEAF))
                if flag
            )
        text = texts.TG_ATTEMPT_NOTICE.format(
            member=_member_display(member),
            action=action,
            actor=actor or texts.ACTOR_UNKNOWN,
        )
        if protection["suppressed_attempts"]:
            text += texts.TG_ATTEMPT_SUPPRESSED.format(count=protection["suppressed_attempts"])
        text += texts.TG_ATTEMPT_COOLDOWN.format(minutes=cooldown // 60)

        await self.repo.mark_notified(guild.id, member.id)
        await notify_guild(self.bot.tg_bot, self.repo, guild.id, guild.name, text)

    async def _notify_undo_failed(
        self, guild: discord.Guild, member: discord.Member, channel, protection
    ) -> None:
        if not protection["notify"]:
            return
        last = protection["last_notified_at"]
        if last is not None and time.time() - last < self.config.mute_guard_notify_cooldown:
            return
        await self.repo.mark_notified(guild.id, member.id)
        await notify_guild(
            self.bot.tg_bot, self.repo, guild.id, guild.name,
            texts.TG_UNMUTE_FAILED.format(
                member=_member_display(member), channel=tg_escape(channel.name)
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
