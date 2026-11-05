"""Telegram-команды сводки голосовых каналов: /online и /online_off.

У каждого сервера чата — своё сообщение-сводка. Если серверов несколько,
команды предлагают выбрать сервер кнопками, а не запускают все сводки разом.

callback_data: ``vd:<действие>:<guild_id>`` (show / hide; hideall и close — с 0).
"""

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot.modules.voice_dashboard import texts
from bot.telegram_bot.actions import button, parse_callback
from bot.telegram_bot.handlers.link import _can_manage

router = Router(name="voice_dashboard")

COG_NAME = "VoiceDashboardCog"
PREFIX = "vd"


async def _show_picker(cog, chat_id: int) -> InlineKeyboardMarkup:
    shown = await cog.shown_guild_ids(chat_id)
    rows = [
        [button(("✓ " if guild.id in shown else "") + guild.name, f"{PREFIX}:show:{guild.id}")]
        for guild in await cog.chat_guilds(chat_id)
    ]
    rows.append([button(texts.BTN_CLOSE, f"{PREFIX}:close:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _hide_picker(cog, chat_id: int) -> InlineKeyboardMarkup:
    shown = await cog.shown_guild_ids(chat_id)
    rows = [
        [button("➖ " + guild.name, f"{PREFIX}:hide:{guild.id}")]
        for guild in await cog.chat_guilds(chat_id) if guild.id in shown
    ]
    rows.append([button(texts.BTN_HIDE_ALL, f"{PREFIX}:hideall:0"),
                 button(texts.BTN_CLOSE, f"{PREFIX}:close:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("online"))
async def cmd_voice(message: Message, discord_bot) -> None:
    cog = discord_bot.get_cog(COG_NAME)
    guilds = await cog.chat_guilds(message.chat.id) if cog else []
    if not guilds:
        await message.answer(texts.DASHBOARD_NO_SERVERS)
        return
    if len(guilds) == 1:
        await cog.send_dashboard(message.chat.id, guilds[0].id)
        return
    await message.answer(texts.PICK_SHOW, reply_markup=await _show_picker(cog, message.chat.id))


@router.message(Command("online_off"))
async def cmd_voice_off(message: Message, bot: Bot, discord_bot) -> None:
    if message.from_user is None or not await _can_manage(bot, message.chat, message.from_user.id):
        await message.answer(texts.DASHBOARD_OFF_FORBIDDEN)
        return
    cog = discord_bot.get_cog(COG_NAME)
    shown = await cog.shown_guild_ids(message.chat.id) if cog else set()
    if not shown:
        await message.answer(texts.DASHBOARD_NOT_FOUND)
        return
    if len(shown) == 1:
        # Успех виден и так — сводка исчезает.
        await cog.remove_dashboards(message.chat.id)
        return
    await message.answer(texts.PICK_HIDE, reply_markup=await _hide_picker(cog, message.chat.id))


@router.callback_query(F.data.startswith(PREFIX + ":"))
async def on_pick(callback: CallbackQuery, bot: Bot, discord_bot) -> None:
    action, (guild_id,) = parse_callback(callback.data)
    chat_id = callback.message.chat.id
    cog = discord_bot.get_cog(COG_NAME)
    if cog is None:
        await callback.answer()
        return

    if action == "close":
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer()
        return

    if action == "show":
        if not await cog.send_dashboard(chat_id, guild_id):
            await callback.answer(texts.PICK_UNAVAILABLE, show_alert=True)
            return
        markup = await _show_picker(cog, chat_id)
    else:
        # Убирать сводки, как и раньше, могут только администраторы чата.
        if not await _can_manage(bot, callback.message.chat, callback.from_user.id):
            await callback.answer(texts.DASHBOARD_OFF_FORBIDDEN, show_alert=True)
            return
        await cog.remove_dashboards(chat_id, None if action == "hideall" else guild_id)
        if not await cog.shown_guild_ids(chat_id):
            await callback.message.edit_text(texts.ALL_HIDDEN, reply_markup=None)
            await callback.answer()
            return
        markup = await _hide_picker(cog, chat_id)
    try:
        await callback.message.edit_reply_markup(reply_markup=markup)
    except TelegramBadRequest:
        pass  # «message is not modified»
    await callback.answer()
