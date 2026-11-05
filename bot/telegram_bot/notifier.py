"""Отправка уведомлений во все Telegram-чаты, привязанные к серверу."""

import html
import logging
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramMigrateToChat
from aiogram.types import InlineKeyboardMarkup

from bot.core import texts
from bot.db.repository import Repository

log = logging.getLogger(__name__)


def tg_escape(value: object) -> str:
    """Экранирует пользовательские данные для parse_mode=HTML."""
    return html.escape(str(value), quote=False)


def tg_user(display_name: str, username: str) -> str:
    """Участник для уведомления: имя жирным, ник — моноширинным (удобно копировать)."""
    if display_name == username:
        return f"<b>{tg_escape(display_name)}</b>"
    return f"<b>{tg_escape(display_name)}</b> (<code>{tg_escape(username)}</code>)"


async def notify_guild(
    tg_bot: Optional[Bot],
    repo: Repository,
    guild_id: int,
    guild_name: str,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    skip_discord_user: Optional[int] = None,
) -> None:
    """Шлёт уведомление во все чаты сервера, с указанием сервера.

    ``text`` начинается со строки-события (что произошло и с кем), затем
    пустая строка и подробности; строка с сервером вставляется после события.
    ``text`` — HTML-разметка Telegram: пользовательские данные в нём должны
    быть экранированы (tg_escape / tg_user). ``reply_markup`` — кнопки действий
    (обрабатываются Telegram-роутером модуля). ``skip_discord_user`` — не слать
    в чаты, которые подключил этот участник Discord (о его собственных
    действиях). Ошибки Telegram не пробрасываются:
    уведомление не должно ломать основное действие в Discord.
    """
    if tg_bot is None:
        return
    # Первая строка — само событие: её видно в превью push-уведомления,
    # поэтому сервер идёт второй строкой, а подробности — ниже.
    headline, _, details = text.partition("\n")
    header = texts.TG_GUILD_HEADER.format(guild_name=tg_escape(guild_name))
    message = f"{headline}\n{header}\n{details}" if details else f"{headline}\n{header}"
    for link in await repo.get_telegram_links(guild_id):
        if skip_discord_user is not None and link["discord_user_id"] == skip_discord_user:
            continue
        chat_id = link["telegram_chat_id"]
        try:
            await tg_bot.send_message(
                chat_id, message, parse_mode="HTML", reply_markup=reply_markup
            )
        except TelegramMigrateToChat as exc:
            # Группа стала супергруппой — переносим связки на новый id.
            await repo.migrate_telegram_chat(chat_id, exc.migrate_to_chat_id)
            try:
                await tg_bot.send_message(
                    exc.migrate_to_chat_id, message, parse_mode="HTML",
                    reply_markup=reply_markup,
                )
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
