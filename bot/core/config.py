"""Конфигурация бота: читается из переменных окружения (.env)."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

DEFAULT_MODULES = ("roles", "mute_guard", "voice_notify", "voice_dashboard")


@dataclass(frozen=True)
class Config:
    discord_token: str
    telegram_token: str
    db_path: str = "data/bot.db"
    # Включённые модули (см. bot/modules/__init__.py).
    enabled_modules: tuple[str, ...] = DEFAULT_MODULES
    # Полное время на верификацию нового участника, сек.
    verification_timeout: int = 600
    # Интервал между напоминаниями, сек.
    reminder_interval: int = 60
    # Срок жизни кода /connect, сек.
    connect_code_ttl: int = 600
    # Порог предупреждения о лимите ролей Discord (жёсткий лимит — 250).
    role_limit_threshold: int = 245
    # Защита от мута: не чаще одного Telegram-уведомления на пользователя, сек.
    mute_guard_notify_cooldown: int = 900
    # Уведомления о заходах в голос: перезаход раньше, чем через столько
    # секунд после выхода, не сообщается.
    voice_notify_cooldown: int = 900
    # Приватный режим: команды на сервере работают только после /unlock
    # с паролем BOT_PASSWORD (уже настроенные серверы пароля не требуют).
    private_mode: bool = False
    bot_password: str = ""
    # Необязательный ID тестового сервера для мгновенной синхронизации slash-команд.
    test_guild_id: int | None = None


def load_config() -> Config:
    discord_token = os.getenv("DISCORD_TOKEN", "").strip()
    telegram_token = os.getenv("TELEGRAM_TOKEN", "").strip()
    if not discord_token or not telegram_token:
        raise RuntimeError(
            "DISCORD_TOKEN и TELEGRAM_TOKEN обязательны — заполните .env (см. .env.example)"
        )

    modules_raw = os.getenv("ENABLED_MODULES", "").strip()
    enabled_modules = (
        tuple(name.strip() for name in modules_raw.split(",") if name.strip())
        if modules_raw
        else DEFAULT_MODULES
    )

    private_mode = os.getenv("PRIVATE_MODE", "").strip().lower() in ("1", "true", "yes", "on")
    bot_password = os.getenv("BOT_PASSWORD", "")
    if private_mode and not bot_password:
        raise RuntimeError("PRIVATE_MODE включён, но BOT_PASSWORD не задан — заполните .env")

    test_guild = os.getenv("TEST_GUILD_ID", "").strip()
    return Config(
        discord_token=discord_token,
        telegram_token=telegram_token,
        db_path=os.getenv("DB_PATH", "data/bot.db"),
        enabled_modules=enabled_modules,
        verification_timeout=int(os.getenv("VERIFICATION_TIMEOUT", "600")),
        reminder_interval=int(os.getenv("REMINDER_INTERVAL", "60")),
        connect_code_ttl=int(os.getenv("CONNECT_CODE_TTL", "600")),
        mute_guard_notify_cooldown=int(os.getenv("MUTE_GUARD_NOTIFY_COOLDOWN", "900")),
        voice_notify_cooldown=int(os.getenv("VOICE_NOTIFY_COOLDOWN", "900")),
        private_mode=private_mode,
        bot_password=bot_password,
        test_guild_id=int(test_guild) if test_guild else None,
    )
