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
    # v2 — отдельная сводка на каждый сервер: (чат, сервер) → сообщение.
    # Старые общие сводки переезжают в voice_dashboards_legacy: при запуске
    # бот удаляет эти сообщения и присылает в чат новые, по серверам.
    [
        "ALTER TABLE voice_dashboards RENAME TO voice_dashboards_legacy",
        """
        CREATE TABLE IF NOT EXISTS voice_guild_dashboards (
            chat_id    INTEGER NOT NULL,
            guild_id   INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (chat_id, guild_id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_voice_guild_dashboards_guild
            ON voice_guild_dashboards (guild_id)
        """,
    ],
]
