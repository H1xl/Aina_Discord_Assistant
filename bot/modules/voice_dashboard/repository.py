"""Доступ к таблицам модуля сводки голосовых каналов."""

from typing import Optional

from bot.db.repository import Repository, _now

DashboardKey = tuple[int, int]  # (chat_id, guild_id)


class VoiceDashboardRepository(Repository):
    async def set_dashboard(self, chat_id: int, guild_id: int, message_id: int) -> None:
        await self._execute(
            """
            INSERT INTO voice_guild_dashboards (chat_id, guild_id, message_id, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id, guild_id) DO UPDATE SET
                message_id = excluded.message_id,
                created_at = excluded.created_at
            """,
            (chat_id, guild_id, message_id, _now()),
        )

    async def get_dashboard(self, chat_id: int, guild_id: int) -> Optional[int]:
        row = await self._fetchone(
            "SELECT message_id FROM voice_guild_dashboards WHERE chat_id = ? AND guild_id = ?",
            (chat_id, guild_id),
        )
        return row["message_id"] if row else None

    async def delete_dashboard(self, chat_id: int, guild_id: int) -> None:
        await self._execute(
            "DELETE FROM voice_guild_dashboards WHERE chat_id = ? AND guild_id = ?",
            (chat_id, guild_id),
        )

    async def chat_dashboards(self, chat_id: int) -> dict[int, int]:
        """guild_id → message_id сводок чата."""
        rows = await self._fetchall(
            "SELECT guild_id, message_id FROM voice_guild_dashboards WHERE chat_id = ?",
            (chat_id,),
        )
        return {row["guild_id"]: row["message_id"] for row in rows}

    async def guild_dashboard_chats(self, guild_id: int) -> list[int]:
        """Чаты, где есть сводка этого сервера."""
        rows = await self._fetchall(
            "SELECT chat_id FROM voice_guild_dashboards WHERE guild_id = ?", (guild_id,)
        )
        return [row["chat_id"] for row in rows]

    async def list_dashboards(self) -> list[DashboardKey]:
        rows = await self._fetchall("SELECT chat_id, guild_id FROM voice_guild_dashboards")
        return [(row["chat_id"], row["guild_id"]) for row in rows]

    async def pop_legacy_dashboards(self) -> dict[int, int]:
        """Забирает общие сводки старого формата (до v2): chat_id → message_id."""
        async with self._conn.execute(
            "DELETE FROM voice_dashboards_legacy RETURNING chat_id, message_id"
        ) as cur:
            rows = list(await cur.fetchall())
        await self._conn.commit()
        return {row["chat_id"]: row["message_id"] for row in rows}
