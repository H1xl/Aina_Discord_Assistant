"""Конфигурация бота: читается из переменных окружения (.env)."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    discord_token: str
    telegram_token: str
    db_path: str = "data/bot.db"
    # Полное время на верификацию нового участника, сек.
    verification_timeout: int = 600
    # Интервал между напоминаниями, сек.
    reminder_interval: int = 60
    # Срок жизни кода /connect, сек.
    connect_code_ttl: int = 600
    # Порог предупреждения о лимите ролей Discord (жёсткий лимит — 250).
    role_limit_threshold: int = 245
    # Необязательный ID тестового сервера для мгновенной синхронизации slash-команд.
    test_guild_id: int | None = None


def load_config() -> Config:
    discord_token = os.getenv("DISCORD_TOKEN", "").strip()
    telegram_token = os.getenv("TELEGRAM_TOKEN", "").strip()
    if not discord_token or not telegram_token:
        raise RuntimeError(
            "DISCORD_TOKEN и TELEGRAM_TOKEN обязательны — заполните .env (см. .env.example)"
        )

    test_guild = os.getenv("TEST_GUILD_ID", "").strip()
    return Config(
        discord_token=discord_token,
        telegram_token=telegram_token,
        db_path=os.getenv("DB_PATH", "data/bot.db"),
        verification_timeout=int(os.getenv("VERIFICATION_TIMEOUT", "600")),
        reminder_interval=int(os.getenv("REMINDER_INTERVAL", "60")),
        connect_code_ttl=int(os.getenv("CONNECT_CODE_TTL", "600")),
        test_guild_id=int(test_guild) if test_guild else None,
    )
