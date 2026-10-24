"""Схема БД модуля уведомлений о заходах в голосовые каналы."""

NAMESPACE = "voice_notify"

MODE_ALL = "all"
MODE_SELECTED = "selected"

# Предел списка отслеживаемых: панель /join_alerts в Telegram остаётся обозримой.
MAX_TRACKED = 25

MIGRATIONS = [
    # v1
    [
        """
        CREATE TABLE IF NOT EXISTS voice_notify_settings (
            guild_id INTEGER PRIMARY KEY,
            mode     TEXT NOT NULL DEFAULT 'all'  -- all | selected
        )
        """,
        # Отслеживаемые участники для режима «только выбранные».
        """
        CREATE TABLE IF NOT EXISTS voice_notify_users (
            guild_id INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
        """,
    ],
]
