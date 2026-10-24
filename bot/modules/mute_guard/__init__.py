"""Модуль «Защита от мута»: /protect, /unprotect, автоотмена помех."""

from bot.modules import ModuleSpec

from .models import MIGRATIONS, NAMESPACE

MODULE = ModuleSpec(
    name=NAMESPACE,
    title="Защита от мута",
    description="снимаю мут, звук и тайм-аут, возвращаю перетащенных, сообщаю в Telegram",
    extensions=("bot.modules.mute_guard.cog",),
    migrations=MIGRATIONS,
    telegram_routers=("bot.modules.mute_guard.telegram",),
)
