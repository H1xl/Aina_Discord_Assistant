"""Схема БД (SQLite). Все таблицы создаются идемпотентно при старте.

Все временные метки хранятся как unix-время (INTEGER, секунды UTC).
"""

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS guild_settings (
        guild_id            INTEGER PRIMARY KEY,
        roles_channel_id    INTEGER NOT NULL,
        pings_channel_id    INTEGER NOT NULL,
        log_channel_id      INTEGER NOT NULL,
        no_role_id          INTEGER,
        notify_on_role_edit INTEGER NOT NULL DEFAULT 0,
        created_at          INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS guild_mandatory_roles (
        guild_id INTEGER NOT NULL,
        role_id  INTEGER NOT NULL,
        PRIMARY KEY (guild_id, role_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS guild_optional_roles (
        guild_id INTEGER NOT NULL,
        role_id  INTEGER NOT NULL,
        PRIMARY KEY (guild_id, role_id)
    )
    """,
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
    CREATE TABLE IF NOT EXISTS pending_verifications (
        guild_id                 INTEGER NOT NULL,
        user_id                  INTEGER NOT NULL,
        joined_at                INTEGER NOT NULL,
        last_reminder_message_id INTEGER,
        reminder_task_active     INTEGER NOT NULL DEFAULT 1,
        status                   TEXT NOT NULL DEFAULT 'pending',
        PRIMARY KEY (guild_id, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS verification_log (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id       INTEGER NOT NULL,
        user_id        INTEGER NOT NULL,
        custom_role_id INTEGER NOT NULL,
        -- Свободный текст из модалки: упоминание в modal технически невозможно.
        inviter        TEXT,
        completed_at   INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS role_edits_log (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id  INTEGER NOT NULL,
        user_id   INTEGER NOT NULL,
        old_name  TEXT NOT NULL,
        old_color TEXT NOT NULL,
        new_name  TEXT NOT NULL,
        new_color TEXT NOT NULL,
        edited_at INTEGER NOT NULL
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
    CREATE INDEX IF NOT EXISTS idx_verification_log_user
        ON verification_log (guild_id, user_id, completed_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_action_log_guild
        ON action_log (guild_id, action, created_at)
    """,
]

# Статусы pending_verifications
STATUS_PENDING = "pending"
STATUS_IN_MODAL = "in_modal"
STATUS_COMPLETED = "completed"
STATUS_KICKED = "kicked"

ACTIVE_STATUSES = (STATUS_PENDING, STATUS_IN_MODAL)
