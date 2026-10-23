"""Отправка уведомлений во все Telegram-чаты, привязанные к серверу."""

import logging
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramMigrateToChat

from bot.core import texts
from bot.db.repository import Repository

log = logging.getLogger(__name__)


async def notify_guild(
    tg_bot: Optional[Bot],
    repo: Repository,
    guild_id: int,
    guild_name: str,
    text: str,
) -> None:
    """Шлёт уведомление во все чаты сервера, с указанием сервера в шапке.

    Ошибки Telegram не пробрасываются: уведомление не должно ломать
    основное действие в Discord.
    """
    if tg_bot is None:
        return
    message = texts.TG_GUILD_HEADER.format(guild_name=guild_name) + "\n" + text
    for chat_id in await repo.get_telegram_chat_ids(guild_id):
        try:
            await tg_bot.send_message(chat_id, message)
        except TelegramMigrateToChat as exc:
            # Группа стала супергруппой — переносим связки на новый id.
            await repo.migrate_telegram_chat(chat_id, exc.migrate_to_chat_id)
            try:
                await tg_bot.send_message(exc.migrate_to_chat_id, message)
            except Exception:
                log.exception("Не удалось отправить в мигрированный чат %s",
                              exc.migrate_to_chat_id)
        except TelegramForbiddenError:
            # Бота удалили из чата или заблокировали в личке — писать туда
            # больше нельзя, связки чата снимаем.
            log.warning("Нет доступа к Telegram-чату %s — отвязываю", chat_id)
            await repo.remove_telegram_chat(chat_id)
            await repo.add_action_log(guild_id, "telegram_auto_unlinked", f"chat_id={chat_id}")
        except Exception:
            log.exception("Не удалось отправить Telegram-уведомление (chat_id=%s)", chat_id)
