"""Модуль «Сводка голосовых каналов»: живое сообщение в Telegram (/online)."""

from bot.modules import ModuleSpec

from .models import MIGRATIONS, NAMESPACE

MODULE = ModuleSpec(
    name=NAMESPACE,
    title="Сводка голосовых каналов",
    description="показываю в Telegram, кто где сидит в голосовых каналах и кто заглушён (/online)",
    extensions=("bot.modules.voice_dashboard.cog",),
    migrations=MIGRATIONS,
    telegram_routers=("bot.modules.voice_dashboard.telegram",),
    telegram_commands=(
        ("online", "Кто сейчас в голосовых каналах"),
        ("online_off", "Убрать сводку голосовых каналов"),
    ),
    default_enabled=True,
)
