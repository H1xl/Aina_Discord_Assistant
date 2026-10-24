"""Telegram-уведомления модуля ролей."""

from typing import Optional

import discord
from aiogram import Bot

from bot.db.repository import Repository
from bot.modules.roles import texts
from bot.telegram_bot.notifier import notify_guild, tg_escape, tg_user


async def send_verification_notice(
    tg_bot: Optional[Bot],
    repo: Repository,
    guild_id: int,
    guild_name: str,
    member: discord.abc.User,
    role_name: str,
    inviter: Optional[str],
    edited: bool = False,
) -> None:
    """Шлёт уведомление о верификации (или изменении роли) во все привязанные чаты."""
    template = texts.TG_ROLE_EDIT_NOTICE if edited else texts.TG_VERIFICATION_NOTICE
    text = template.format(
        member=tg_user(member.display_name, member.name),
        role_name=tg_escape(role_name),
        inviter=tg_escape(inviter) if inviter else f"<i>{texts.INVITER_NOT_SPECIFIED}</i>",
    )
    await notify_guild(tg_bot, repo, guild_id, guild_name, text)
