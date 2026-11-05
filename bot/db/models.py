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
    # v2 — связка «многие-ко-многим»: несколько чатов на сервер
    # и несколько серверов в одном чате.
    [
        """
        CREATE TABLE IF NOT EXISTS telegram_links (
            guild_id         INTEGER NOT NULL,
            telegram_chat_id INTEGER NOT NULL,
            chat_title       TEXT,
            linked_at        INTEGER NOT NULL,
            PRIMARY KEY (guild_id, telegram_chat_id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_telegram_links_chat
            ON telegram_links (telegram_chat_id)
        """,
        """
        INSERT OR IGNORE INTO telegram_links (guild_id, telegram_chat_id, linked_at)
        SELECT guild_id, telegram_chat_id, linked_at FROM guild_telegram_link
        """,
        "DROP TABLE IF EXISTS guild_telegram_link",
    ],
    # v3 — включение модулей по серверам (/modules).
    [
        """
        CREATE TABLE IF NOT EXISTS guild_modules (
            guild_id INTEGER NOT NULL,
            module   TEXT NOT NULL,
            enabled  INTEGER NOT NULL,
            PRIMARY KEY (guild_id, module)
        )
        """,
    ],
    # v4 — подтверждённые Telegram-аккаунты администраторов (для кнопок
    # действий в уведомлениях) и тип кода /connect.
    [
        """
        CREATE TABLE IF NOT EXISTS telegram_admins (
            guild_id        INTEGER NOT NULL,
            tg_user_id      INTEGER NOT NULL,
            discord_user_id INTEGER NOT NULL,
            linked_at       INTEGER NOT NULL,
            PRIMARY KEY (guild_id, tg_user_id)
        )
        """,
        # Тип кода. Сейчас всегда «chat»: подтверждение админа (бывший
        # /telegram-me) объединено с /connect.
        "ALTER TABLE connect_codes ADD COLUMN kind TEXT NOT NULL DEFAULT 'chat'",
    ],
    # v5 — кто подключил чат (Discord ID автора кода /connect): о его
    # собственных заходах в голос этот чат не уведомляется.
    [
        "ALTER TABLE telegram_links ADD COLUMN discord_user_id INTEGER",
        # Для уже привязанных личных чатов владелец известен по подтверждённому
        # Telegram-аккаунту администратора (id личного чата = id пользователя).
        """
        UPDATE telegram_links SET discord_user_id = (
            SELECT a.discord_user_id FROM telegram_admins a
            WHERE a.guild_id = telegram_links.guild_id
              AND a.tg_user_id = telegram_links.telegram_chat_id
        )
        WHERE discord_user_id IS NULL
        """,
    ],
]
