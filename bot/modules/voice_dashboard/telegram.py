"""Telegram-команды сводки голосовых каналов: /online и /online_off.

У каждого сервера чата — своё сообщение-сводка.
"""

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.modules.voice_dashboard import texts
from bot.telegram_bot.handlers.link import _can_manage

router = Router(name="voice_dashboard")

COG_NAME = "VoiceDashboardCog"


@router.message(Command("online"))
async def cmd_voice(message: Message, discord_bot) -> None:
    cog = discord_bot.get_cog(COG_NAME)
    if cog is None or not await cog.send_dashboards(message.chat.id):
        await message.answer(texts.DASHBOARD_NO_SERVERS)


@router.message(Command("online_off"))
async def cmd_voice_off(message: Message, bot: Bot, discord_bot) -> None:
    if message.from_user is None or not await _can_manage(bot, message.chat, message.from_user.id):
        await message.answer(texts.DASHBOARD_OFF_FORBIDDEN)
        return
    cog = discord_bot.get_cog(COG_NAME)
    # Успех виден и так — сводки исчезают; отвечаем, только если их не было.
    if cog is None or not await cog.remove_dashboards(message.chat.id):
        await message.answer(texts.DASHBOARD_NOT_FOUND)
