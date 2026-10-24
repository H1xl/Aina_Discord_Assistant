"""Модуль «Уведомления о заходах в голос»: кто и куда зашёл — в Telegram."""

from bot.modules import ModuleSpec

from .models import MIGRATIONS, NAMESPACE

MODULE = ModuleSpec(
    name=NAMESPACE,
    title="Уведомления о заходах в голос",
    description="сообщаю в Telegram, кто зашёл в голосовой канал",
    extensions=("bot.modules.voice_notify.cog",),
    migrations=MIGRATIONS,
    default_enabled=True,
)
