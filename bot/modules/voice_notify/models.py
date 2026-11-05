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
    # v2 — автоудаление уведомлений после того, как канал опустел.
    [
        "ALTER TABLE voice_notify_settings ADD COLUMN autodelete_minutes INTEGER NOT NULL DEFAULT 0",
        # Отправленные уведомления о заходах; delete_at проставляется, когда
        # канал опустел, и сбрасывается, если в него снова зашли.
        """
        CREATE TABLE IF NOT EXISTS voice_notify_messages (
            guild_id   INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            chat_id    INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            sent_at    INTEGER NOT NULL,
            delete_at  INTEGER,
            PRIMARY KEY (chat_id, message_id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_voice_notify_messages_channel
            ON voice_notify_messages (guild_id, channel_id)
        """,
    ],
]

# Варианты автоудаления, мин. (0 — выключено). Не больше суток: Telegram
# не даёт ботам удалять сообщения старше 48 часов.
AUTODELETE_OPTIONS = (0, 15, 60, 180, 720, 1440)
# Записи старше этого уже не удалить — просто забываем их.
DELETE_LIMIT_SECONDS = 47 * 3600
