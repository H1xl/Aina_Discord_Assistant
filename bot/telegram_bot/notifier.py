"""Отправка уведомлений в привязанный Telegram-чат из Discord-части."""

import logging
from typing import Optional

from aiogram import Bot

from bot.core import texts
from bot.db.repository import Repository

log = logging.getLogger(__name__)


async def send_verification_notice(
    tg_bot: Optional[Bot],
    repo: Repository,
    guild_id: int,
    guild_name: str,
    member_display: str,
    role_name: str,
    inviter: Optional[str],
) -> None:
    """Шлёт уведомление об успешной верификации, если чат привязан."""
    if tg_bot is None:
        return
    chat_id = await repo.get_telegram_chat_id(guild_id)
    if chat_id is None:
        return
    text = texts.TG_VERIFICATION_NOTICE.format(
        guild_name=guild_name,
        member=member_display,
        role_name=role_name,
        inviter=inviter or texts.INVITER_NOT_SPECIFIED,
    )
    try:
        await tg_bot.send_message(chat_id, text)
    except Exception:
        # Бот могли удалить из чата — верификацию это ломать не должно.
        log.exception("Не удалось отправить Telegram-уведомление (chat_id=%s)", chat_id)
