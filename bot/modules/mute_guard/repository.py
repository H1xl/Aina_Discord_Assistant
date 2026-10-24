"""Доступ к таблицам модуля защиты от мута."""

from typing import Optional

import aiosqlite

from bot.db.repository import Repository, _now


class MuteGuardRepository(Repository):
    async def upsert_protection(
        self,
        guild_id: int,
        user_id: int,
        protected_by: int,
        expires_at: Optional[int],
        notify: bool,
        guards: str,
    ) -> None:
        await self._execute(
            """
            INSERT INTO mute_protections
                (guild_id, user_id, protected_by, created_at, expires_at, notify,
                 last_notified_at, suppressed_attempts, guards)
            VALUES (?, ?, ?, ?, ?, ?, NULL, 0, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                protected_by = excluded.protected_by,
                created_at = excluded.created_at,
                expires_at = excluded.expires_at,
                notify = excluded.notify,
                last_notified_at = NULL,
                suppressed_attempts = 0,
                guards = excluded.guards
            """,
            (guild_id, user_id, protected_by, _now(), expires_at, 1 if notify else 0, guards),
        )

    async def extend_protection(
        self, guild_id: int, user_id: int, seconds: int
    ) -> Optional[int]:
        """Продлевает срочную защиту; возвращает новый expires_at (None — защиты нет
        или она бессрочная)."""
        row = await self.get_protection(guild_id, user_id)
        if row is None or row["expires_at"] is None:
            return None
        expires_at = max(row["expires_at"], _now()) + seconds
        await self._execute(
            "UPDATE mute_protections SET expires_at = ? WHERE guild_id = ? AND user_id = ?",
            (expires_at, guild_id, user_id),
        )
        return expires_at

    async def protected_guild_ids(self) -> list[int]:
        rows = await self._fetchall(
            """
            SELECT DISTINCT guild_id FROM mute_protections
            WHERE expires_at IS NULL OR expires_at > ?
            """,
            (_now(),),
        )
        return [row["guild_id"] for row in rows]

    async def get_protection(self, guild_id: int, user_id: int) -> Optional[aiosqlite.Row]:
        """Действующая (не истёкшая) защита участника."""
        return await self._fetchone(
            """
            SELECT * FROM mute_protections
            WHERE guild_id = ? AND user_id = ? AND (expires_at IS NULL OR expires_at > ?)
            """,
            (guild_id, user_id, _now()),
        )

    async def list_protections(self, guild_id: int) -> list[aiosqlite.Row]:
        return await self._fetchall(
            """
            SELECT * FROM mute_protections
            WHERE guild_id = ? AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY created_at
            """,
            (guild_id, _now()),
        )

    async def remove_protection(self, guild_id: int, user_id: int) -> bool:
        return bool(await self._execute(
            "DELETE FROM mute_protections WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ))

    async def pop_expired(self) -> list[aiosqlite.Row]:
        """Удаляет истёкшие защиты и возвращает их."""
        async with self._conn.execute(
            "DELETE FROM mute_protections WHERE expires_at IS NOT NULL AND expires_at <= ? "
            "RETURNING *",
            (_now(),),
        ) as cur:
            rows = list(await cur.fetchall())
        await self._conn.commit()
        return rows

    async def mark_notified(self, guild_id: int, user_id: int) -> None:
        await self._execute(
            """
            UPDATE mute_protections SET last_notified_at = ?, suppressed_attempts = 0
            WHERE guild_id = ? AND user_id = ?
            """,
            (_now(), guild_id, user_id),
        )

    async def add_suppressed_attempt(self, guild_id: int, user_id: int) -> None:
        await self._execute(
            """
            UPDATE mute_protections SET suppressed_attempts = suppressed_attempts + 1
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        )
