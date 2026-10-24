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
    # v2 — от каких действий защищать (выбирается в /protect).
    [
        "ALTER TABLE mute_protections ADD COLUMN guards TEXT NOT NULL DEFAULT 'mute,deaf'",
    ],
]

# Защищаемые действия (значения колонки guards через запятую).
GUARD_MUTE = "mute"              # server mute — снимаем
GUARD_DEAF = "deaf"              # server deafen — снимаем
GUARD_MOVE = "move"              # перетаскивание в другой канал — возвращаем
GUARD_DISCONNECT = "disconnect"  # отключение от голоса — только уведомляем
GUARD_TIMEOUT = "timeout"        # тайм-аут — снимаем

ALL_GUARDS = (GUARD_MUTE, GUARD_DEAF, GUARD_MOVE, GUARD_DISCONNECT, GUARD_TIMEOUT)
DEFAULT_GUARDS = (GUARD_MUTE, GUARD_DEAF)


def parse_guards(value: str) -> set[str]:
    return {guard for guard in (value or "").split(",") if guard in ALL_GUARDS}


def serialize_guards(guards) -> str:
    return ",".join(guard for guard in ALL_GUARDS if guard in guards)
