"""Мелкие Discord-хелперы, общие для ядра и модулей."""

import asyncio
import logging
from typing import Optional

import discord

from bot.core import texts

log = logging.getLogger(__name__)


def is_admin(interaction: discord.Interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.administrator)


async def respond(
    interaction: discord.Interaction,
    description: str,
    ephemeral: bool = True,
    title: Optional[str] = None,
    colour: int = texts.EMBED_COLOR,
) -> None:
    """Ephemeral-ответ, работающий и для первого ответа, и для followup."""
    embed = discord.Embed(title=title, description=description, colour=colour)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral)
    except discord.HTTPException as exc:
        log.warning("Не удалось ответить на interaction: %s", exc)


async def deny_admin(interaction: discord.Interaction) -> None:
    await respond(interaction, texts.ADMIN_ONLY, colour=texts.EMBED_COLOR_ERROR)


async def with_backoff(factory, attempts: int = 3):
    """Вызывает coroutine-фабрику с экспоненциальным backoff на HTTP-ошибках.

    discord.py сам ждёт rate limit, но при сетевых/5xx-ошибках повторяем.
    """
    delay = 2.0
    for attempt in range(attempts):
        try:
            return await factory()
        except discord.NotFound:
            return None
        except discord.Forbidden:
            raise
        except discord.HTTPException as exc:
            if attempt == attempts - 1:
                log.warning("Discord API error after retries: %s", exc)
                return None
            await asyncio.sleep(delay)
            delay *= 2


async def expire_view_message(
    message: Optional[discord.Message], description: str = texts.VIEW_EXPIRED
) -> None:
    """Заменяет протухшее ephemeral-меню на сообщение об истечении времени."""
    if message is None:
        return
    try:
        await message.edit(
            embed=discord.Embed(description=description, colour=texts.EMBED_COLOR_WARNING),
            view=None,
        )
    except discord.HTTPException:
        pass
