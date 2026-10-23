"""Схема БД модуля защиты от мута (SQLite)."""

NAMESPACE = "mute_guard"

MIGRATIONS = [
    # v1
    [
        """
        CREATE TABLE IF NOT EXISTS mute_protections (
            guild_id            INTEGER NOT NULL,
            user_id             INTEGER NOT NULL,
            protected_by        INTEGER NOT NULL,
            created_at          INTEGER NOT NULL,
            expires_at          INTEGER,            -- NULL — бессрочно
            notify              INTEGER NOT NULL DEFAULT 1,
            -- Антиспам уведомлений: когда последний раз писали в Telegram
            -- и сколько попыток с тех пор промолчали.
            last_notified_at    INTEGER,
            suppressed_attempts INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        )
        """,
    ],
]
