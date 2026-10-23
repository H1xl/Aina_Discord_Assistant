"""Отправка уведомлений в привязанный к серверу Telegram-чат."""

import logging
from typing import Optional

from aiogram import Bot

from bot.db.repository import Repository

log = logging.getLogger(__name__)


async def notify_guild(
    tg_bot: Optional[Bot], repo: Repository, guild_id: int, text: str
) -> None:
    """Шлёт уведомление в привязанный чат сервера, если он есть.

    Ошибки Telegram не пробрасываются: уведомление не должно ломать
    основное действие в Discord.
    """
    if tg_bot is None:
        return
    chat_id = await repo.get_telegram_chat_id(guild_id)
    if chat_id is None:
        return
    try:
        await tg_bot.send_message(chat_id, text)
    except Exception:
        # Бота могли удалить из чата.
        log.exception("Не удалось отправить Telegram-уведомление (chat_id=%s)", chat_id)
