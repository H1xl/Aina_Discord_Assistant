"""Доступ к таблицам модуля сводки голосовых каналов."""

from typing import Optional

from bot.db.repository import Repository, _now


class VoiceDashboardRepository(Repository):
    async def set_dashboard(self, chat_id: int, message_id: int) -> None:
        await self._execute(
            """
            INSERT INTO voice_dashboards (chat_id, message_id, created_at) VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                message_id = excluded.message_id,
                created_at = excluded.created_at
            """,
            (chat_id, message_id, _now()),
        )

    async def get_dashboard(self, chat_id: int) -> Optional[int]:
        row = await self._fetchone(
            "SELECT message_id FROM voice_dashboards WHERE chat_id = ?", (chat_id,)
        )
        return row["message_id"] if row else None

    async def delete_dashboard(self, chat_id: int) -> None:
        await self._execute("DELETE FROM voice_dashboards WHERE chat_id = ?", (chat_id,))

    async def list_dashboards(self) -> dict[int, int]:
        """chat_id → message_id всех сводок."""
        rows = await self._fetchall("SELECT chat_id, message_id FROM voice_dashboards")
        return {row["chat_id"]: row["message_id"] for row in rows}
