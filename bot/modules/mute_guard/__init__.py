"""Модуль «Защита от мута»: /protect, /unprotect, автоснятие заглушения."""

from bot.modules import ModuleSpec

from .models import MIGRATIONS, NAMESPACE

MODULE = ModuleSpec(
    name=NAMESPACE,
    title="Защита от мута",
    description="снимаю с защищённых мут и отключение звука, сообщаю в Telegram",
    extensions=("bot.modules.mute_guard.cog",),
    migrations=MIGRATIONS,
)
