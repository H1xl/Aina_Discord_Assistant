"""Асинхронный доступ к SQLite (aiosqlite). Единственная точка работы с БД."""

import time
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from .models import ACTIVE_STATUSES, SCHEMA, STATUS_PENDING


def _now() -> int:
    return int(time.time())


class Repository:
    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    @classmethod
    async def connect(cls, db_path: str) -> "Repository":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        for statement in SCHEMA:
            await conn.execute(statement)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self._conn.close()

    async def _fetchone(self, sql: str, params: tuple = ()) -> Optional[aiosqlite.Row]:
        async with self._conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def _fetchall(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        async with self._conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def _execute(self, sql: str, params: tuple = ()) -> None:
        await self._conn.execute(sql, params)
        await self._conn.commit()

    # ------------------------------------------------------------------
    # guild_settings
    # ------------------------------------------------------------------
    async def get_settings(self, guild_id: int) -> Optional[aiosqlite.Row]:
        return await self._fetchone(
            "SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)
        )

    async def upsert_settings(
        self,
        guild_id: int,
        roles_channel_id: int,
        pings_channel_id: int,
        log_channel_id: int,
        no_role_id: Optional[int],
    ) -> None:
        await self._execute(
            """
            INSERT INTO guild_settings
                (guild_id, roles_channel_id, pings_channel_id, log_channel_id,
                 no_role_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                roles_channel_id = excluded.roles_channel_id,
                pings_channel_id = excluded.pings_channel_id,
                log_channel_id = excluded.log_channel_id,
                no_role_id = excluded.no_role_id
            """,
            (guild_id, roles_channel_id, pings_channel_id, log_channel_id,
             no_role_id, _now()),
        )

    async def set_no_role_id(self, guild_id: int, no_role_id: int) -> None:
        await self._execute(
            "UPDATE guild_settings SET no_role_id = ? WHERE guild_id = ?",
            (no_role_id, guild_id),
        )

    # ------------------------------------------------------------------
    # Обязательные / опциональные роли
    # ------------------------------------------------------------------
    async def set_mandatory_roles(self, guild_id: int, role_ids: list[int]) -> None:
        await self._conn.execute(
            "DELETE FROM guild_mandatory_roles WHERE guild_id = ?", (guild_id,)
        )
        await self._conn.executemany(
            "INSERT OR IGNORE INTO guild_mandatory_roles (guild_id, role_id) VALUES (?, ?)",
            [(guild_id, rid) for rid in role_ids],
        )
        await self._conn.commit()

    async def get_mandatory_roles(self, guild_id: int) -> list[int]:
        rows = await self._fetchall(
            "SELECT role_id FROM guild_mandatory_roles WHERE guild_id = ?", (guild_id,)
        )
        return [row["role_id"] for row in rows]

    async def set_optional_roles(self, guild_id: int, role_ids: list[int]) -> None:
        await self._conn.execute(
            "DELETE FROM guild_optional_roles WHERE guild_id = ?", (guild_id,)
        )
        await self._conn.executemany(
            "INSERT OR IGNORE INTO guild_optional_roles (guild_id, role_id) VALUES (?, ?)",
            [(guild_id, rid) for rid in role_ids],
        )
        await self._conn.commit()

    async def get_optional_roles(self, guild_id: int) -> list[int]:
        rows = await self._fetchall(
            "SELECT role_id FROM guild_optional_roles WHERE guild_id = ?", (guild_id,)
        )
        return [row["role_id"] for row in rows]

    # ------------------------------------------------------------------
    # Telegram-связка и коды /connect
    # ------------------------------------------------------------------
    async def upsert_telegram_link(self, guild_id: int, telegram_chat_id: int) -> None:
        await self._execute(
            """
            INSERT INTO guild_telegram_link (guild_id, telegram_chat_id, linked_at)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                telegram_chat_id = excluded.telegram_chat_id,
                linked_at = excluded.linked_at
            """,
            (guild_id, telegram_chat_id, _now()),
        )

    async def get_telegram_chat_id(self, guild_id: int) -> Optional[int]:
        row = await self._fetchone(
            "SELECT telegram_chat_id FROM guild_telegram_link WHERE guild_id = ?",
            (guild_id,),
        )
        return row["telegram_chat_id"] if row else None

    async def create_connect_code(
        self, guild_id: int, code: str, created_by: int, ttl_seconds: int
    ) -> None:
        await self._execute(
            """
            INSERT OR REPLACE INTO connect_codes
                (code, guild_id, created_by, expires_at, used)
            VALUES (?, ?, ?, ?, 0)
            """,
            (code, guild_id, created_by, _now() + ttl_seconds),
        )

    async def use_connect_code(self, code: str) -> Optional[int]:
        """Атомарно помечает код использованным.

        Возвращает guild_id, если код существовал, не истёк и не был использован.
        """
        async with self._conn.execute(
            """
            UPDATE connect_codes SET used = 1
            WHERE code = ? AND used = 0 AND expires_at > ?
            RETURNING guild_id
            """,
            (code, _now()),
        ) as cur:
            row = await cur.fetchone()
        await self._conn.commit()
        return row["guild_id"] if row else None

    async def cleanup_expired_codes(self) -> None:
        await self._execute(
            "DELETE FROM connect_codes WHERE expires_at <= ? OR used = 1", (_now(),)
        )

    # ------------------------------------------------------------------
    # pending_verifications
    # ------------------------------------------------------------------
    async def upsert_pending(self, guild_id: int, user_id: int, joined_at: int) -> None:
        await self._execute(
            """
            INSERT INTO pending_verifications
                (guild_id, user_id, joined_at, last_reminder_message_id,
                 reminder_task_active, status)
            VALUES (?, ?, ?, NULL, 1, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                joined_at = excluded.joined_at,
                last_reminder_message_id = NULL,
                reminder_task_active = 1,
                status = excluded.status
            """,
            (guild_id, user_id, joined_at, STATUS_PENDING),
        )

    async def get_pending(self, guild_id: int, user_id: int) -> Optional[aiosqlite.Row]:
        return await self._fetchone(
            "SELECT * FROM pending_verifications WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )

    async def set_pending_status(self, guild_id: int, user_id: int, status: str) -> None:
        await self._execute(
            "UPDATE pending_verifications SET status = ? WHERE guild_id = ? AND user_id = ?",
            (status, guild_id, user_id),
        )

    async def set_last_reminder(
        self, guild_id: int, user_id: int, message_id: Optional[int]
    ) -> None:
        await self._execute(
            """
            UPDATE pending_verifications SET last_reminder_message_id = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (message_id, guild_id, user_id),
        )

    async def set_reminder_task_active(
        self, guild_id: int, user_id: int, active: bool
    ) -> None:
        await self._execute(
            """
            UPDATE pending_verifications SET reminder_task_active = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (1 if active else 0, guild_id, user_id),
        )

    async def delete_pending(self, guild_id: int, user_id: int) -> None:
        await self._execute(
            "DELETE FROM pending_verifications WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )

    async def get_active_pendings(self) -> list[aiosqlite.Row]:
        """Активные верификации для восстановления таймеров после рестарта."""
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        return await self._fetchall(
            f"""
            SELECT * FROM pending_verifications
            WHERE status IN ({placeholders}) AND reminder_task_active = 1
            """,
            ACTIVE_STATUSES,
        )

    # ------------------------------------------------------------------
    # Журналы
    # ------------------------------------------------------------------
    async def add_verification(
        self, guild_id: int, user_id: int, custom_role_id: int, inviter: Optional[str]
    ) -> None:
        await self._execute(
            """
            INSERT INTO verification_log
                (guild_id, user_id, custom_role_id, inviter, completed_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (guild_id, user_id, custom_role_id, inviter, _now()),
        )

    async def get_last_verification(
        self, guild_id: int, user_id: int
    ) -> Optional[aiosqlite.Row]:
        return await self._fetchone(
            """
            SELECT * FROM verification_log
            WHERE guild_id = ? AND user_id = ?
            ORDER BY completed_at DESC, id DESC LIMIT 1
            """,
            (guild_id, user_id),
        )

    async def add_role_edit(
        self,
        guild_id: int,
        user_id: int,
        old_name: str,
        old_color: str,
        new_name: str,
        new_color: str,
    ) -> None:
        await self._execute(
            """
            INSERT INTO role_edits_log
                (guild_id, user_id, old_name, old_color, new_name, new_color, edited_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (guild_id, user_id, old_name, old_color, new_name, new_color, _now()),
        )

    async def add_action_log(
        self, guild_id: Optional[int], action: str, details: str = ""
    ) -> None:
        await self._execute(
            "INSERT INTO action_log (guild_id, action, details, created_at) VALUES (?, ?, ?, ?)",
            (guild_id, action, details, _now()),
        )

    async def get_stats(self, guild_id: int, since: int) -> dict[str, Any]:
        completed = await self._fetchone(
            "SELECT COUNT(*) AS c FROM verification_log WHERE guild_id = ? AND completed_at >= ?",
            (guild_id, since),
        )
        kicked = await self._fetchone(
            """
            SELECT COUNT(*) AS c FROM action_log
            WHERE guild_id = ? AND action = 'kick' AND created_at >= ?
            """,
            (guild_id, since),
        )
        return {"completed": completed["c"], "kicked": kicked["c"]}
