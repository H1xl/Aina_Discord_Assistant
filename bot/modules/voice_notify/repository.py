"""Доступ к таблицам модуля уведомлений о заходах в голос."""

from bot.db.repository import Repository

from .models import MODE_ALL


class VoiceNotifyRepository(Repository):
    async def get_mode(self, guild_id: int) -> str:
        row = await self._fetchone(
            "SELECT mode FROM voice_notify_settings WHERE guild_id = ?", (guild_id,)
        )
        return row["mode"] if row else MODE_ALL

    async def get_tracked_users(self, guild_id: int) -> list[int]:
        rows = await self._fetchall(
            "SELECT user_id FROM voice_notify_users WHERE guild_id = ?", (guild_id,)
        )
        return [row["user_id"] for row in rows]

    async def set_mode(self, guild_id: int, mode: str) -> None:
        await self._execute(
            """
            INSERT INTO voice_notify_settings (guild_id, mode) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET mode = excluded.mode
            """,
            (guild_id, mode),
        )

    async def add_tracked_user(self, guild_id: int, user_id: int) -> None:
        await self._execute(
            "INSERT OR IGNORE INTO voice_notify_users (guild_id, user_id) VALUES (?, ?)",
            (guild_id, user_id),
        )

    async def remove_tracked_user(self, guild_id: int, user_id: int) -> None:
        await self._execute(
            "DELETE FROM voice_notify_users WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )

    async def save_settings(self, guild_id: int, mode: str, user_ids: list[int]) -> None:
        await self._conn.execute(
            """
            INSERT INTO voice_notify_settings (guild_id, mode) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET mode = excluded.mode
            """,
            (guild_id, mode),
        )
        await self._conn.execute(
            "DELETE FROM voice_notify_users WHERE guild_id = ?", (guild_id,)
        )
        await self._conn.executemany(
            "INSERT OR IGNORE INTO voice_notify_users (guild_id, user_id) VALUES (?, ?)",
            [(guild_id, user_id) for user_id in user_ids],
        )
        await self._conn.commit()
