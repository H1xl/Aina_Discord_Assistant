"""Привязка Telegram-чата к Discord-серверу по коду из /connect."""

import logging
import re

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from bot.core import texts
from bot.db.repository import Repository

log = logging.getLogger(__name__)

router = Router(name="link")

CODE_RE = re.compile(r"^\s*([A-Za-z0-9]{6})\s*$")


async def _try_link(message: Message, code: str, repo: Repository, discord_bot) -> None:
    guild_id = await repo.use_connect_code(code.upper())
    if guild_id is None:
        await message.answer(texts.TG_CODE_INVALID)
        return

    await repo.upsert_telegram_link(guild_id, message.chat.id)
    await repo.add_action_log(
        guild_id, "telegram_linked", f"chat_id={message.chat.id}"
    )

    guild = discord_bot.get_guild(guild_id)
    guild_name = guild.name if guild else f"ID {guild_id}"
    await message.answer(texts.TG_LINKED.format(guild_name=guild_name))
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


@router.message(F.text.regexp(CODE_RE))
async def plain_code(message: Message, repo: Repository, discord_bot) -> None:
    # Обычное сообщение, состоящее из одного 6-символьного кода.
    await _try_link(message, CODE_RE.match(message.text).group(1), repo, discord_bot)
