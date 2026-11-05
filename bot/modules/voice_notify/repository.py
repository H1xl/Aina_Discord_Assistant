"""Доступ к таблицам модуля уведомлений о заходах в голос."""

import aiosqlite

from bot.db.repository import Repository, _now

from .models import DELETE_LIMIT_SECONDS, MODE_ALL


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

    # ------------------------------------------------------------------
    # Автоудаление уведомлений
    # ------------------------------------------------------------------
    async def get_autodelete(self, guild_id: int) -> int:
        """Через сколько минут после опустения канала удалять уведомления (0 — нет)."""
        row = await self._fetchone(
            "SELECT autodelete_minutes FROM voice_notify_settings WHERE guild_id = ?", (guild_id,)
        )
        return row["autodelete_minutes"] if row else 0

    async def set_autodelete(self, guild_id: int, minutes: int) -> None:
        await self._execute(
            """
            INSERT INTO voice_notify_settings (guild_id, autodelete_minutes) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET autodelete_minutes = excluded.autodelete_minutes
            """,
            (guild_id, minutes),
        )
        if not minutes:
            # Автоудаление выключили — уже отправленные уведомления остаются.
            await self._execute(
                "DELETE FROM voice_notify_messages WHERE guild_id = ?", (guild_id,)
            )

    async def add_messages(
        self, guild_id: int, channel_id: int, sent: list[tuple[int, int]]
    ) -> None:
        now = _now()
        await self._conn.executemany(
            """
            INSERT OR IGNORE INTO voice_notify_messages
                (guild_id, channel_id, chat_id, message_id, sent_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [(guild_id, channel_id, chat_id, message_id, now) for chat_id, message_id in sent],
        )
        await self._conn.commit()

    async def channels_with_messages(self) -> list[aiosqlite.Row]:
        """Каналы, по которым есть что удалять; delete_at — NULL, если не назначено."""
        return await self._fetchall(
            """
            SELECT guild_id, channel_id, MIN(delete_at) AS delete_at
            FROM voice_notify_messages GROUP BY guild_id, channel_id
            """
        )

    async def schedule_channel(self, guild_id: int, channel_id: int, delete_at: int) -> None:
        await self._execute(
            """
            UPDATE voice_notify_messages SET delete_at = ?
            WHERE guild_id = ? AND channel_id = ? AND delete_at IS NULL
            """,
            (delete_at, guild_id, channel_id),
        )

    async def unschedule_channel(self, guild_id: int, channel_id: int) -> None:
        await self._execute(
            "UPDATE voice_notify_messages SET delete_at = NULL WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        )

    async def pop_due_messages(self) -> list[aiosqlite.Row]:
        """Забирает уведомления, которые пора удалить; слишком старые просто забывает."""
        now = _now()
        await self._execute(
            "DELETE FROM voice_notify_messages WHERE sent_at < ?", (now - DELETE_LIMIT_SECONDS,)
        )
        async with self._conn.execute(
            "DELETE FROM voice_notify_messages WHERE delete_at IS NOT NULL AND delete_at <= ? "
            "RETURNING chat_id, message_id",
            (now,),
        ) as cur:
            rows = list(await cur.fetchall())
        await self._conn.commit()
        return rows
