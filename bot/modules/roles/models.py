"""Схема БД модуля ролей (SQLite)."""

NAMESPACE = "roles"

MIGRATIONS = [
    # v1 — исходная схема (до перехода на модули).
    [
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
        CREATE INDEX IF NOT EXISTS idx_verification_log_user
            ON verification_log (guild_id, user_id, completed_at)
        """,
    ],
    # v2 — явная привязка «участник ↔ именная роль». Раньше владелец
    # определялся по последней записи verification_log; теперь роль можно
    # привязать и импортом (/import-roles) — для ролей, созданных вручную.
    [
        """
        CREATE TABLE IF NOT EXISTS personal_roles (
            guild_id INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            role_id  INTEGER NOT NULL,
            source   TEXT NOT NULL,  -- verification | import
            bound_at INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id),
            UNIQUE (guild_id, role_id)
        )
        """,
        """
        INSERT OR IGNORE INTO personal_roles (guild_id, user_id, role_id, source, bound_at)
        SELECT v.guild_id, v.user_id, v.custom_role_id, 'verification', v.completed_at
        FROM verification_log v
        WHERE v.id = (
            SELECT v2.id FROM verification_log v2
            WHERE v2.guild_id = v.guild_id AND v2.user_id = v.user_id
            ORDER BY v2.completed_at DESC, v2.id DESC LIMIT 1
        )
        """,
    ],
]

# Статусы pending_verifications
STATUS_PENDING = "pending"
STATUS_IN_MODAL = "in_modal"
STATUS_COMPLETED = "completed"
STATUS_KICKED = "kicked"

ACTIVE_STATUSES = (STATUS_PENDING, STATUS_IN_MODAL)

# Источник привязки personal_roles
SOURCE_VERIFICATION = "verification"
SOURCE_IMPORT = "import"
