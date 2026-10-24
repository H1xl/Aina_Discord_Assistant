"""Привязка и отвязка Telegram-чата к Discord-серверам.

Связка «многие-ко-многим»: один чат может получать уведомления нескольких
серверов, а у сервера может быть несколько чатов.
"""

import logging
import re

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.core import texts
from bot.db.models import CODE_KIND_ADMIN
from bot.db.repository import Repository

log = logging.getLogger(__name__)

router = Router(name="link")

CODE_RE = re.compile(r"^\s*([A-Za-z0-9]{6})\s*$")
UNLINK_PREFIX = "unlink:"
UNLINK_CANCEL = "unlink-cancel"


def _guild_name(discord_bot, guild_id: int) -> str:
    guild = discord_bot.get_guild(guild_id)
    return guild.name if guild else f"ID {guild_id}"


def _chat_title(chat: Chat) -> str:
    return chat.title or chat.full_name or texts.TG_CHAT_UNTITLED


async def _servers_list(repo: Repository, discord_bot, chat_id: int) -> str:
    guild_ids = await repo.get_linked_guild_ids(chat_id)
    return "\n".join(f"• {_guild_name(discord_bot, gid)}" for gid in guild_ids)


async def _can_manage(bot: Bot, chat: Chat, user_id: int) -> bool:
    """В личке управляет сам собеседник, в группах — только администраторы."""
    if chat.type == "private":
        return True
    member = await bot.get_chat_member(chat.id, user_id)
    return member.status in ("creator", "administrator")


async def _try_link(message: Message, code: str, repo: Repository, discord_bot) -> None:
    row = await repo.use_connect_code(code.upper())
    if row is None:
        await message.answer(texts.TG_CODE_INVALID)
        return
    guild_id = row["guild_id"]

    if row["kind"] == CODE_KIND_ADMIN:
        await repo.bind_telegram_admin(guild_id, message.from_user.id, row["created_by"])
        await repo.add_action_log(
            guild_id, "telegram_admin_bound",
            f"tg_user={message.from_user.id} discord_user={row['created_by']}",
        )
        await message.answer(texts.TG_ADMIN_BOUND.format(
            guild_name=_guild_name(discord_bot, guild_id)
        ))
        return

    # Код привязки чата, введённый в личке, заодно подтверждает и самого
    # администратора, получившего код, — чтобы кнопки работали сразу.
    if message.chat.type == "private" and message.from_user is not None:
        await repo.bind_telegram_admin(guild_id, message.from_user.id, row["created_by"])

    is_new = await repo.add_telegram_link(guild_id, message.chat.id, _chat_title(message.chat))
    await repo.add_action_log(
        guild_id, "telegram_linked", f"chat_id={message.chat.id}"
    )

    template = texts.TG_LINKED if is_new else texts.TG_ALREADY_LINKED
    await message.answer(template.format(
        guild_name=_guild_name(discord_bot, guild_id),
        servers=await _servers_list(repo, discord_bot, message.chat.id),
    ))
    log.info("Telegram-чат %s привязан к серверу %s", message.chat.id, guild_id)


@router.message(Command("start"))
async def cmd_start(
    message: Message, command: CommandObject, repo: Repository, discord_bot
) -> None:
    # `/start КОД` или просто /start с подсказкой.
    if command.args and CODE_RE.match(command.args):
        await _try_link(message, CODE_RE.match(command.args).group(1), repo, discord_bot)
    else:
        await message.answer(texts.TG_START_NO_CODE)


@router.message(Command("servers"))
async def cmd_servers(message: Message, repo: Repository, discord_bot) -> None:
    servers = await _servers_list(repo, discord_bot, message.chat.id)
    await message.answer(
        texts.TG_SERVERS_LIST.format(servers=servers) if servers else texts.TG_SERVERS_EMPTY
    )


@router.message(Command("unlink"))
async def cmd_unlink(message: Message, bot: Bot, repo: Repository, discord_bot) -> None:
    if message.from_user is None or not await _can_manage(bot, message.chat, message.from_user.id):
        await message.answer(texts.TG_UNLINK_FORBIDDEN)
        return
    guild_ids = await repo.get_linked_guild_ids(message.chat.id)
    if not guild_ids:
        await message.answer(texts.TG_SERVERS_EMPTY)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        *[
            [InlineKeyboardButton(
                text=_guild_name(discord_bot, gid), callback_data=f"{UNLINK_PREFIX}{gid}"
            )]
            for gid in guild_ids
        ],
        [InlineKeyboardButton(text=texts.TG_UNLINK_CANCEL, callback_data=UNLINK_CANCEL)],
    ])
    await message.answer(texts.TG_UNLINK_PROMPT, reply_markup=keyboard)


@router.callback_query(F.data == UNLINK_CANCEL)
async def cb_unlink_cancel(callback: CallbackQuery) -> None:
    await callback.message.edit_text(texts.TG_UNLINK_CANCELLED)
    await callback.answer()


@router.callback_query(F.data.startswith(UNLINK_PREFIX))
async def cb_unlink(callback: CallbackQuery, bot: Bot, repo: Repository, discord_bot) -> None:
    chat = callback.message.chat
    if not await _can_manage(bot, chat, callback.from_user.id):
        await callback.answer(texts.TG_UNLINK_FORBIDDEN, show_alert=True)
        return
    guild_id = int(callback.data.removeprefix(UNLINK_PREFIX))
    if not await repo.remove_telegram_link(guild_id, chat.id):
        await callback.message.edit_text(texts.TG_UNLINK_MISSING)
        await callback.answer()
        return
    await repo.add_action_log(
        guild_id, "telegram_unlinked", f"chat_id={chat.id} by_tg={callback.from_user.id}"
    )
    await callback.message.edit_text(
        texts.TG_UNLINK_DONE.format(guild_name=_guild_name(discord_bot, guild_id))
    )
    await callback.answer()
    log.info("Telegram-чат %s отвязан от сервера %s", chat.id, guild_id)


# StateFilter(None): пока бот ждёт от пользователя ввода (например, ник
# в /join_alerts), короткое слово — это ответ, а не код привязки.
@router.message(StateFilter(None), F.text.regexp(CODE_RE))
async def plain_code(message: Message, repo: Repository, discord_bot) -> None:
    # Обычное сообщение, состоящее из одного 6-символьного кода.
    await _try_link(message, CODE_RE.match(message.text).group(1), repo, discord_bot)
