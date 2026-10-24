"""/join_alerts в Telegram: включение уведомлений о заходах и фильтр участников.

Единственное место настройки этих уведомлений; включение модуля — то же,
что в /modules в Discord.
Доступно только подтверждённым администраторам (/telegram-me).

callback_data: ``ja:<действие>:<guild_id>[:<user_id>]``.
"""

import logging
from typing import Optional

import discord
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ForceReply, InlineKeyboardMarkup, Message

from bot.db.repository import Repository
from bot.modules.voice_notify import texts
from bot.modules.voice_notify.models import MAX_TRACKED, MODE_ALL, MODE_SELECTED, NAMESPACE
from bot.modules.voice_notify.repository import VoiceNotifyRepository
from bot.telegram_bot.actions import authorize_admin, button, parse_callback
from bot.telegram_bot.notifier import tg_escape, tg_user

log = logging.getLogger(__name__)

router = Router(name=NAMESPACE)

PREFIX = "ja"
# Сколько найденных участников показывать кнопками.
SEARCH_LIMIT = 10


class JoinAlertsStates(StatesGroup):
    waiting_name = State()


def _data(action: str, guild_id: int, user_id: Optional[int] = None) -> str:
    parts = [PREFIX, action, str(guild_id)]
    if user_id is not None:
        parts.append(str(user_id))
    return ":".join(parts)


async def _admin_member(repo: Repository, discord_bot, guild_id: int, tg_user_id: int):
    """Discord-администратор за Telegram-аккаунтом или None."""
    guild = discord_bot.get_guild(guild_id)
    discord_id = await repo.get_telegram_admin(guild_id, tg_user_id)
    member = guild.get_member(discord_id) if guild and discord_id else None
    if member is None or not member.guild_permissions.administrator:
        return None
    return member


def _member_label(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    return tg_user(member.display_name, member.name) if member else f"ID {user_id}"


async def render_panel(discord_bot, guild: discord.Guild) -> tuple[str, InlineKeyboardMarkup]:
    vrepo = VoiceNotifyRepository(discord_bot.db)
    enabled = await discord_bot.is_module_enabled(guild.id, NAMESPACE)
    mode = await vrepo.get_mode(guild.id)
    user_ids = await vrepo.get_tracked_users(guild.id)

    users = "\n".join(f"• {_member_label(guild, uid)}" for uid in user_ids) or texts.TG_USERS_NONE
    text = texts.TG_PANEL.format(
        guild_name=tg_escape(guild.name),
        status=texts.TG_STATUS_ON if enabled else texts.TG_STATUS_OFF,
        mode=texts.MODE_SELECTED_LABEL if mode == MODE_SELECTED else texts.MODE_ALL_LABEL,
        users=users,
        count=len(user_ids),
        max=MAX_TRACKED,
    )
    if mode == MODE_SELECTED and not user_ids:
        text += texts.TG_EMPTY_WARNING
    if mode == MODE_ALL and user_ids:
        text += texts.TG_ALL_MODE_HINT

    mark = "✓ "
    rows = [
        [button(texts.TG_BTN_TURN_OFF if enabled else texts.TG_BTN_TURN_ON,
                _data("off" if enabled else "on", guild.id))],
        [
            button((mark if mode == MODE_ALL else "") + texts.MODE_ALL_LABEL, _data("all", guild.id)),
            button((mark if mode == MODE_SELECTED else "") + texts.MODE_SELECTED_LABEL,
                   _data("sel", guild.id)),
        ],
        [button(texts.TG_BTN_ADD, _data("add", guild.id))],
    ]
    if user_ids:
        rows[2].append(button(texts.TG_BTN_REMOVE, _data("rm", guild.id)))
    rows.append([button(texts.TG_BTN_CLOSE, _data("close", guild.id))])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_panel(message: Message, discord_bot, guild: discord.Guild, edit: bool) -> None:
    text, markup = await render_panel(discord_bot, guild)
    try:
        if edit:
            await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        else:
            await message.answer(text, parse_mode="HTML", reply_markup=markup)
    except TelegramBadRequest as exc:
        # «message is not modified» — повторное нажатие на уже выбранное.
        if "not modified" not in str(exc):
            raise


async def _refresh_stored_panel(bot: Bot, state: FSMContext, discord_bot, guild) -> None:
    """Обновляет панель, из которой пользователь пошёл добавлять участника."""
    data = await state.get_data()
    chat_id, message_id = data.get("panel_chat_id"), data.get("panel_message_id")
    if not chat_id or not message_id:
        return
    text, markup = await render_panel(discord_bot, guild)
    try:
        await bot.edit_message_text(
            text, chat_id=chat_id, message_id=message_id, parse_mode="HTML", reply_markup=markup
        )
    except TelegramBadRequest as exc:
        log.info("Не удалось обновить панель /join_alerts: %s", exc)


async def _add_user(discord_bot, guild: discord.Guild, member: discord.Member, admin_id: int) -> str:
    """Добавляет участника в отслеживаемые; возвращает текст ответа (HTML)."""
    vrepo = VoiceNotifyRepository(discord_bot.db)
    user_ids = await vrepo.get_tracked_users(guild.id)
    label = tg_user(member.display_name, member.name)
    if member.id in user_ids:
        return texts.TG_ALREADY_TRACKED.format(member=label)
    if len(user_ids) >= MAX_TRACKED:
        return texts.TG_LIMIT_REACHED.format(max=MAX_TRACKED)
    await vrepo.add_tracked_user(guild.id, member.id)
    await vrepo.add_action_log(
        guild.id, "voice_notify_user_added", f"user_id={member.id} by={admin_id} via=telegram"
    )
    return texts.TG_ADDED.format(member=label)


# ---------------------------------------------------------------------------
# Команда
# ---------------------------------------------------------------------------
@router.message(Command("join_alerts"))
async def cmd_join_alerts(
    message: Message, repo: Repository, discord_bot, state: FSMContext
) -> None:
    await state.clear()
    if message.from_user is None or discord_bot.get_module(NAMESPACE) is None:
        return
    guilds = []
    for guild_id in await repo.get_linked_guild_ids(message.chat.id):
        if await _admin_member(repo, discord_bot, guild_id, message.from_user.id):
            guilds.append(discord_bot.get_guild(guild_id))
    if not guilds:
        await message.answer(texts.TG_NO_ACCESS)
        return
    if len(guilds) == 1:
        await _show_panel(message, discord_bot, guilds[0], edit=False)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [button(guild.name, _data("open", guild.id))] for guild in guilds
    ])
    await message.answer(texts.TG_CHOOSE_SERVER, reply_markup=keyboard)


# ---------------------------------------------------------------------------
# Кнопки панели
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith(PREFIX + ":"))
async def on_action(
    callback: CallbackQuery, bot: Bot, repo: Repository, discord_bot, state: FSMContext
) -> None:
    action, ids = parse_callback(callback.data)
    guild_id = ids[0]
    guild = discord_bot.get_guild(guild_id)
    if guild is None:
        await callback.answer(texts.TG_GUILD_UNAVAILABLE, show_alert=True)
        return
    admin = await authorize_admin(callback, repo, discord_bot, guild_id)
    if admin is None:
        return
    vrepo = VoiceNotifyRepository(discord_bot.db)

    if action == "close":
        await state.clear()
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer()
        return

    if action in ("on", "off"):
        enabled = action == "on"
        await discord_bot.set_module_enabled(guild_id, NAMESPACE, enabled)
        await vrepo.add_action_log(
            guild_id, "modules", f"{NAMESPACE}={int(enabled)} by={admin.id} via=telegram"
        )
    elif action in ("all", "sel"):
        mode = MODE_ALL if action == "all" else MODE_SELECTED
        await vrepo.set_mode(guild_id, mode)
        await vrepo.add_action_log(
            guild_id, "voice_notify_settings", f"mode={mode} by={admin.id} via=telegram"
        )
    elif action == "add":
        await state.set_state(JoinAlertsStates.waiting_name)
        await state.update_data(
            guild_id=guild_id,
            panel_chat_id=callback.message.chat.id,
            panel_message_id=callback.message.message_id,
        )
        await callback.message.answer(
            texts.TG_ASK_NAME.format(guild_name=tg_escape(guild.name)),
            parse_mode="HTML",
            reply_markup=ForceReply(input_field_placeholder=texts.TG_NAME_PLACEHOLDER),
        )
        await callback.answer()
        return
    elif action == "rm":
        user_ids = await vrepo.get_tracked_users(guild_id)
        rows = [
            [button("➖ " + (m.display_name if (m := guild.get_member(uid)) else f"ID {uid}"),
                    _data("del", guild_id, uid))]
            for uid in user_ids
        ]
        rows.append([button(texts.TG_BTN_BACK, _data("open", guild_id))])
        await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await callback.answer(texts.TG_REMOVE_HINT)
        return
    elif action == "del":
        await vrepo.remove_tracked_user(guild_id, ids[1])
        await vrepo.add_action_log(
            guild_id, "voice_notify_user_removed", f"user_id={ids[1]} by={admin.id} via=telegram"
        )
    elif action == "pick":
        member = guild.get_member(ids[1])
        if member is None:
            await callback.answer(texts.TG_MEMBER_GONE, show_alert=True)
            return
        result = await _add_user(discord_bot, guild, member, admin.id)
        await callback.message.edit_text(result, parse_mode="HTML", reply_markup=None)
        await _refresh_stored_panel(bot, state, discord_bot, guild)
        await state.clear()
        await callback.answer()
        return

    # on/off/all/sel/del/open — перерисовываем панель на месте.
    await _show_panel(callback.message, discord_bot, guild, edit=True)
    await callback.answer()


# ---------------------------------------------------------------------------
# Поиск участника по нику
# ---------------------------------------------------------------------------
@router.message(JoinAlertsStates.waiting_name, F.text)
async def on_name(
    message: Message, bot: Bot, repo: Repository, discord_bot, state: FSMContext
) -> None:
    data = await state.get_data()
    guild = discord_bot.get_guild(data.get("guild_id", 0))
    if guild is None or message.from_user is None:
        await state.clear()
        return
    admin = await _admin_member(repo, discord_bot, guild.id, message.from_user.id)
    if admin is None:
        await state.clear()
        await message.answer(texts.TG_NO_ACCESS)
        return

    query = message.text.strip().lstrip("@").casefold()
    matches = [
        member for member in guild.members
        if not member.bot and any(
            query in (name or "").casefold()
            for name in (member.display_name, member.global_name, member.name)
        )
    ]
    if not matches:
        await state.clear()
        await message.answer(
            texts.TG_NOT_FOUND.format(query=tg_escape(message.text.strip())), parse_mode="HTML"
        )
        return
    if len(matches) == 1:
        result = await _add_user(discord_bot, guild, matches[0], admin.id)
        await message.answer(result, parse_mode="HTML")
        await _refresh_stored_panel(bot, state, discord_bot, guild)
        await state.clear()
        return

    # Несколько совпадений — выбор кнопкой (состояние держим, чтобы потом
    # обновить панель).
    shown = sorted(matches, key=lambda m: m.display_name.casefold())[:SEARCH_LIMIT]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [button(f"{m.display_name} (@{m.name})", _data("pick", guild.id, m.id))] for m in shown
    ])
    text = texts.TG_PICK.format(count=len(matches))
    if len(matches) > SEARCH_LIMIT:
        text += texts.TG_PICK_MORE.format(limit=SEARCH_LIMIT)
    await message.answer(text, reply_markup=keyboard)
