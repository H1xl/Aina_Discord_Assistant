"""Доступ к таблицам модуля ролей."""

from typing import Any, Optional

import aiosqlite

from bot.db.repository import Repository, _now

from .models import ACTIVE_STATUSES, STATUS_PENDING


class RolesRepository(Repository):
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
    # personal_roles: кому принадлежит именная роль
    # ------------------------------------------------------------------
    async def get_personal_role_id(self, guild_id: int, user_id: int) -> Optional[int]:
        row = await self._fetchone(
            "SELECT role_id FROM personal_roles WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        return row["role_id"] if row else None

    async def get_personal_roles(self, guild_id: int) -> dict[int, int]:
        """Все привязки сервера: role_id → user_id."""
        rows = await self._fetchall(
            "SELECT user_id, role_id FROM personal_roles WHERE guild_id = ?", (guild_id,)
        )
        return {row["role_id"]: row["user_id"] for row in rows}

    async def bind_personal_roles(
        self, guild_id: int, bindings: list[tuple[int, int]], source: str
    ) -> None:
        """Привязывает роли к участникам: список пар (user_id, role_id).

        У участника — одна именная роль, у роли — один владелец, поэтому
        прежние привязки этого участника и этой роли снимаются.
        """
        now = _now()
        for user_id, role_id in bindings:
            await self._conn.execute(
                "DELETE FROM personal_roles WHERE guild_id = ? AND (user_id = ? OR role_id = ?)",
                (guild_id, user_id, role_id),
            )
            await self._conn.execute(
                """
                INSERT INTO personal_roles (guild_id, user_id, role_id, source, bound_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (guild_id, user_id, role_id, source, now),
            )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # Роли ушедших участников (возврат при перезаходе)
    # ------------------------------------------------------------------
    async def save_role_snapshot(self, guild_id: int, user_id: int, role_ids: list[int]) -> None:
        await self._execute(
            """
            INSERT INTO member_role_snapshots (guild_id, user_id, role_ids, saved_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                role_ids = excluded.role_ids,
                saved_at = excluded.saved_at
            """,
            (guild_id, user_id, ",".join(map(str, role_ids)), _now()),
        )

    async def pop_role_snapshot(self, guild_id: int, user_id: int) -> list[int]:
        """Забирает сохранённые роли участника (запись удаляется)."""
        async with self._conn.execute(
            "DELETE FROM member_role_snapshots WHERE guild_id = ? AND user_id = ? "
            "RETURNING role_ids",
            (guild_id, user_id),
        ) as cur:
            row = await cur.fetchone()
        await self._conn.commit()
        if row is None or not row["role_ids"]:
            return []
        return [int(value) for value in row["role_ids"].split(",")]

    # ------------------------------------------------------------------
    # pending_verifications
    # ------------------------------------------------------------------
    async def set_pending_joined_at(self, guild_id: int, user_id: int, joined_at: int) -> None:
        """Сдвигает отсчёт верификации (продление из Telegram)."""
        await self._execute(
            "UPDATE pending_verifications SET joined_at = ? WHERE guild_id = ? AND user_id = ?",
            (joined_at, guild_id, user_id),
        )

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
