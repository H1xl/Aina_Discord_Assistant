"""Кнопки действий под Telegram-уведомлениями защиты от мута.

callback_data: ``mg:<действие>:<guild>:<участник>[:<виновник>]``. Три
snowflake в десятичном виде не помещаются в 64 байта, поэтому id здесь
в base36 (свой разбор, не actions.parse_callback).

Действия: u — снять защиту, x — продлить на час, t — тайм-аут виновнику,
k — кикнуть виновника (спросить подтверждение), K — кик подтверждён,
b — отмена подтверждения (вернуть исходные кнопки).
"""

import datetime as dt
import logging
from typing import Optional

import discord
from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from bot.db.repository import Repository
from bot.modules.mute_guard import texts
from bot.modules.mute_guard.models import NAMESPACE
from bot.modules.mute_guard.repository import MuteGuardRepository
from bot.telegram_bot.actions import (
    authorize_admin,
    button,
    confirm_markup,
    fail_action,
    finish_action,
)
from bot.telegram_bot.notifier import tg_escape

log = logging.getLogger(__name__)

router = Router(name=NAMESPACE)

PREFIX = "mg"
EXTEND_SECONDS = 3600
ACTOR_TIMEOUT_MINUTES = 10
_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def _b36(value: int) -> str:
    out = ""
    while True:
        value, rest = divmod(value, 36)
        out = _DIGITS[rest] + out
        if not value:
            return out


def _data(action: str, *ids: int) -> str:
    return ":".join([PREFIX, action, *(_b36(i) for i in ids)])


def _parse(data: str) -> tuple[str, list[int]]:
    _, action, *ids = data.split(":")
    return action, [int(value, 36) for value in ids]


def attempt_keyboard(
    guild_id: int, user_id: int, actor_id: Optional[int], can_extend: bool
) -> InlineKeyboardMarkup:
    """Кнопки под уведомлением о попытке помешать защищённому участнику."""
    rows = [[button(texts.BTN_UNPROTECT, _data("u", guild_id, user_id))]]
    if can_extend:
        rows[0].append(button(texts.BTN_EXTEND, _data("x", guild_id, user_id)))
    if actor_id:
        rows.append([button(texts.BTN_TIMEOUT_ACTOR, _data("t", guild_id, user_id, actor_id))])
        rows.append([button(texts.BTN_KICK_ACTOR, _data("k", guild_id, user_id, actor_id))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _append_result(callback: CallbackQuery, result: str, admin: discord.Member) -> None:
    """Дописывает результат, оставляя кнопки (для неокончательных действий)."""
    line = texts.TG_ACTION_DONE.format(result=result, admin=tg_escape(admin.display_name))
    try:
        await callback.message.edit_text(
            callback.message.html_text + "\n" + line,
            parse_mode="HTML",
            reply_markup=callback.message.reply_markup,
        )
    except Exception as exc:
        log.info("Не удалось обновить уведомление: %s", exc)
    await callback.answer()


@router.callback_query(F.data.startswith(PREFIX + ":"))
async def on_action(callback: CallbackQuery, repo: Repository, discord_bot) -> None:
    action, ids = _parse(callback.data)
    guild_id, user_id = ids[0], ids[1]
    actor_id = ids[2] if len(ids) > 2 else None

    admin = await authorize_admin(callback, repo, discord_bot, guild_id)
    if admin is None:
        return
    if not await discord_bot.is_module_enabled(guild_id, NAMESPACE):
        await fail_action(callback, texts.TG_MODULE_OFF)
        return

    mg_repo = MuteGuardRepository(discord_bot.db)
    guild = admin.guild

    if action == "u":
        if not await mg_repo.remove_protection(guild_id, user_id):
            await fail_action(callback, texts.TG_ACTION_FAILED.format(reason=texts.FAIL_NOT_PROTECTED))
            return
        await mg_repo.add_action_log(guild_id, "mute_unprotect", f"user_id={user_id} by={admin.id} via=tg")
        await finish_action(callback, texts.DONE_UNPROTECT, admin)

    elif action == "x":
        protection = await mg_repo.get_protection(guild_id, user_id)
        if protection is None:
            await fail_action(callback, texts.TG_ACTION_FAILED.format(reason=texts.FAIL_NOT_PROTECTED))
            return
        if protection["expires_at"] is None:
            await fail_action(callback, texts.TG_ACTION_FAILED.format(reason=texts.FAIL_INFINITE))
            return
        await mg_repo.extend_protection(guild_id, user_id, EXTEND_SECONDS)
        await mg_repo.add_action_log(guild_id, "mute_extend", f"user_id={user_id} by={admin.id} via=tg")
        await _append_result(callback, texts.DONE_EXTEND, admin)

    elif action in ("t", "k", "K"):
        actor = guild.get_member(actor_id) if actor_id else None
        if actor is None:
            await fail_action(callback, texts.TG_ACTION_FAILED.format(reason=texts.TG_ACTION_MEMBER_GONE))
            return
        if actor.guild_permissions.administrator:
            await fail_action(callback, texts.TG_ACTION_FAILED.format(reason=texts.FAIL_ACTOR_ADMIN))
            return
        if action == "k":
            await callback.message.edit_reply_markup(reply_markup=confirm_markup(
                _data("K", guild_id, user_id, actor_id), _data("b", guild_id, user_id, actor_id)
            ))
            await callback.answer()
            return
        reason = (texts.ACTOR_TIMEOUT_REASON if action == "t" else texts.ACTOR_KICK_REASON).format(
            admin=admin.display_name
        )
        try:
            if action == "t":
                cog = discord_bot.get_cog("MuteGuardCog")
                if cog is not None:
                    await cog.timeout_actor(actor, ACTOR_TIMEOUT_MINUTES, reason)
                else:
                    await actor.timeout(dt.timedelta(minutes=ACTOR_TIMEOUT_MINUTES), reason=reason)
            else:
                await actor.kick(reason=reason)
        except discord.Forbidden:
            await fail_action(callback, texts.TG_ACTION_FAILED.format(reason=texts.TG_ACTION_NO_PERMS))
            return
        except discord.HTTPException as exc:
            await fail_action(callback, texts.TG_ACTION_FAILED.format(reason=str(exc)[:100]))
            return
        await mg_repo.add_action_log(
            guild_id, "actor_timeout" if action == "t" else "actor_kick",
            f"actor_id={actor.id} by={admin.id} via=tg",
        )
        if action == "t":
            await _append_result(callback, texts.DONE_TIMEOUT, admin)
        else:
            await finish_action(callback, texts.DONE_KICK, admin)

    elif action == "b":
        protection = await mg_repo.get_protection(guild_id, user_id)
        await callback.message.edit_reply_markup(reply_markup=attempt_keyboard(
            guild_id, user_id, actor_id,
            can_extend=protection is not None and protection["expires_at"] is not None,
        ))
        await callback.answer()

    else:
        await callback.answer()
