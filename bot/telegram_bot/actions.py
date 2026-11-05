"""Кнопки действий в Telegram-уведомлениях: авторизация и оформление результата.

Нажимать кнопки могут только Telegram-аккаунты, подтверждённые как
администраторы сервера (прислали свой код /connect), и только пока у этого
человека есть право администратора на сервере.

callback_data: ``<префикс модуля>:<действие>:<guild_id>:<user_id>``.
Telegram ограничивает её 64 байтами: двух десятичных snowflake (до 19 цифр)
и короткого действия хватает, а трём уже тесно — модулю, которому нужно
больше ID, стоит кодировать их компактнее (например, base36, как в mute_guard).
"""

import logging
from typing import Optional

import discord
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.core import texts
from bot.db.repository import Repository
from bot.telegram_bot.notifier import tg_escape

log = logging.getLogger(__name__)


def parse_callback(data: str) -> tuple[str, list[int]]:
    """``prefix:action:1:2`` → (``action``, [1, 2])."""
    _, action, *ids = data.split(":")
    return action, [int(value) for value in ids]


def button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def confirm_markup(yes_data: str, no_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        button(texts.TG_ACTION_CONFIRM, yes_data),
        button(texts.TG_ACTION_CANCEL, no_data),
    ]])


async def authorize_admin(
    callback: CallbackQuery, repo: Repository, discord_bot, guild_id: int
) -> Optional[discord.Member]:
    """Discord-администратор, нажавший кнопку, или None (с всплывающим отказом)."""
    guild = discord_bot.get_guild(guild_id)
    discord_id = await repo.get_telegram_admin(guild_id, callback.from_user.id)
    member = guild.get_member(discord_id) if guild and discord_id else None
    if member is None or not member.guild_permissions.administrator:
        await callback.answer(texts.TG_ACTION_FORBIDDEN, show_alert=True)
        return None
    return member


async def finish_action(callback: CallbackQuery, result: str, admin: discord.Member) -> None:
    """Дописывает результат к уведомлению и убирает кнопки.

    ``result`` — HTML, пользовательские данные в нём уже экранированы.
    """
    line = texts.TG_ACTION_DONE.format(result=result, admin=tg_escape(admin.display_name))
    try:
        await callback.message.edit_text(
            callback.message.html_text + "\n\n" + line, parse_mode="HTML", reply_markup=None
        )
    except TelegramBadRequest as exc:
        log.info("Не удалось обновить уведомление: %s", exc)
    await callback.answer()


async def fail_action(callback: CallbackQuery, reason: str) -> None:
    """Всплывающее сообщение об ошибке; уведомление и кнопки остаются как были."""
    await callback.answer(reason, show_alert=True)
