"""Telegram-уведомления модуля ролей."""

from typing import Optional

import discord
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup

from bot.db.repository import Repository
from bot.modules.roles import texts
from bot.telegram_bot.actions import button
from bot.telegram_bot.notifier import notify_guild, tg_escape, tg_user

# Префикс callback_data кнопок модуля (обработчики — bot/modules/roles/telegram.py).
CALLBACK_PREFIX = "rl"


def join_markup(guild_id: int, user_id: int) -> InlineKeyboardMarkup:
    """Кнопки под уведомлением о новичке, проходящем верификацию."""
    ids = f"{guild_id}:{user_id}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            button(texts.TG_BTN_EXTEND, f"{CALLBACK_PREFIX}:ext:{ids}"),
            button(texts.TG_BTN_SKIP, f"{CALLBACK_PREFIX}:skip:{ids}"),
        ],
        [button(texts.TG_BTN_KICK, f"{CALLBACK_PREFIX}:kick:{ids}")],
    ])


def _account_age(member: discord.Member) -> str:
    days = (discord.utils.utcnow() - member.created_at).days
    return texts.TG_ACCOUNT_AGE.format(date=member.created_at.strftime("%d.%m.%Y"), days=days)


async def send_join_notice(
    tg_bot: Optional[Bot],
    repo: Repository,
    guild: discord.Guild,
    member: discord.Member,
    minutes: int,
) -> None:
    """Новый участник зашёл и проходит верификацию — с кнопками действий."""
    text = texts.TG_JOIN_NOTICE.format(
        member=tg_user(member.display_name, member.name),
        created=_account_age(member),
        minutes=minutes,
    )
    await notify_guild(
        tg_bot, repo, guild.id, guild.name, text, reply_markup=join_markup(guild.id, member.id)
    )


async def send_returned_notice(
    tg_bot: Optional[Bot],
    repo: Repository,
    guild: discord.Guild,
    member: discord.Member,
    roles: list[discord.Role],
) -> None:
    """Участник вернулся на сервер — роли восстановлены без верификации."""
    text = texts.TG_RETURNED_NOTICE.format(
        member=tg_user(member.display_name, member.name),
        roles=", ".join(f"«{tg_escape(role.name)}»" for role in roles),
    )
    await notify_guild(tg_bot, repo, guild.id, guild.name, text)


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
