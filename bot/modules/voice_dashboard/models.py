"""Схема БД модуля сводки голосовых каналов."""

NAMESPACE = "voice_dashboard"

MIGRATIONS = [
    # v1 — сообщения-сводки в Telegram-чатах (по одному на чат),
    # чтобы продолжать их редактировать после рестарта.
    [
        """
        CREATE TABLE IF NOT EXISTS voice_dashboards (
            chat_id    INTEGER PRIMARY KEY,
            message_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        )
        """,
    ],
]
