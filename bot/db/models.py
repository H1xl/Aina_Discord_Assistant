"""Схема БД ядра (SQLite): привязка Telegram, коды /connect, журнал действий.

Таблицы модулей описаны в их собственных ``models.py``.
Все временные метки хранятся как unix-время (INTEGER, секунды UTC).
"""

CORE_NAMESPACE = "core"

CORE_MIGRATIONS = [
    # v1 — исходная схема (до перехода на модули).
    [
        """
        CREATE TABLE IF NOT EXISTS guild_telegram_link (
            guild_id         INTEGER PRIMARY KEY,
            telegram_chat_id INTEGER NOT NULL,
            linked_at        INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS connect_codes (
            code       TEXT PRIMARY KEY,
            guild_id   INTEGER NOT NULL,
            created_by INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            used       INTEGER NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS action_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id   INTEGER,
            action     TEXT NOT NULL,
            details    TEXT,
            created_at INTEGER NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_action_log_guild
            ON action_log (guild_id, action, created_at)
        """,
    ],
]
