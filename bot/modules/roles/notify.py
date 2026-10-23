"""Telegram-уведомления модуля ролей."""

from typing import Optional

from aiogram import Bot

from bot.db.repository import Repository
from bot.modules.roles import texts
from bot.telegram_bot.notifier import notify_guild


async def send_verification_notice(
    tg_bot: Optional[Bot],
    repo: Repository,
    guild_id: int,
    guild_name: str,
    member_display: str,
    role_name: str,
    inviter: Optional[str],
) -> None:
    """Шлёт уведомление об успешной верификации в привязанный чат."""
    text = texts.TG_VERIFICATION_NOTICE.format(
        guild_name=guild_name,
        member=member_display,
        role_name=role_name,
        inviter=inviter or texts.INVITER_NOT_SPECIFIED,
    )
    await notify_guild(tg_bot, repo, guild_id, text)
