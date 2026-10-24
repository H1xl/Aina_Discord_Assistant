"""Кнопки уведомления о новичке: продлить, пропустить верификацию, кикнуть."""

import logging

import discord
from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.db.repository import Repository
from bot.modules.roles import texts
from bot.modules.roles.notify import CALLBACK_PREFIX, join_markup
from bot.telegram_bot.actions import (
    authorize_admin,
    confirm_markup,
    fail_action,
    finish_action,
    parse_callback,
)
from bot.telegram_bot.notifier import tg_escape

log = logging.getLogger(__name__)

router = Router(name="roles")

EXTEND_SECONDS = 600


@router.callback_query(F.data.startswith(f"{CALLBACK_PREFIX}:"))
async def on_action(callback: CallbackQuery, repo: Repository, discord_bot) -> None:
    action, (guild_id, user_id) = parse_callback(callback.data)
    guild = discord_bot.get_guild(guild_id)
    cog = discord_bot.get_cog("VerificationCog")
    if guild is None or cog is None or not await discord_bot.is_module_enabled(guild_id, "roles"):
        await fail_action(callback, texts.TG_MODULE_UNAVAILABLE)
        return

    ids = f"{guild_id}:{user_id}"
    # Отмена подтверждения кика — просто возвращаем исходные кнопки.
    if action == "kickno":
        await callback.message.edit_reply_markup(reply_markup=join_markup(guild_id, user_id))
        await callback.answer()
        return

    admin = await authorize_admin(callback, repo, discord_bot, guild_id)
    if admin is None:
        return

    if action == "kick":
        await callback.message.edit_reply_markup(reply_markup=confirm_markup(
            f"{CALLBACK_PREFIX}:kickok:{ids}", f"{CALLBACK_PREFIX}:kickno:{ids}"
        ))
        await callback.answer(texts.TG_KICK_CONFIRM)
        return

    if action == "ext":
        minutes = await cog.extend_verification(guild, user_id, EXTEND_SECONDS)
        if minutes is None:
            await fail_action(callback, texts.TG_VERIFICATION_OVER)
            return
        await finish_action(callback, texts.TG_RESULT_EXTENDED.format(minutes=minutes), admin)
        return

    member = guild.get_member(user_id)
    if member is None:
        await fail_action(callback, texts.TG_ACTION_FAILED.format(reason=texts.TG_ACTION_MEMBER_GONE))
        return

    try:
        if action == "skip":
            done = await cog.skip_verification(guild, member, admin)
            result = texts.TG_RESULT_SKIPPED
        elif action == "kickok":
            done = await cog.kick_pending(guild, member, admin)
            result = texts.TG_RESULT_KICKED.format(member=tg_escape(member.display_name))
        else:
            await callback.answer()
            return
    except discord.Forbidden:
        await fail_action(callback, texts.TG_ACTION_FAILED.format(reason=texts.TG_ACTION_NO_PERMS))
        return
    except discord.HTTPException as exc:
        log.warning("Действие %s из Telegram не выполнено: %s", action, exc)
        await fail_action(callback, texts.TG_ACTION_FAILED.format(reason=exc.text or exc))
        return

    if not done:
        await fail_action(callback, texts.TG_VERIFICATION_OVER)
        return
    await finish_action(callback, result, admin)
