"""Репозиторий ядра: Telegram-связки, коды /connect, журнал действий.

Репозитории модулей наследуются от него, чтобы иметь доступ к общему
журналу действий.
"""

import time
from typing import Optional

from .database import BaseRepository


def _now() -> int:
    return int(time.time())


class Repository(BaseRepository):
    # ------------------------------------------------------------------
    # Telegram-связка
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

    # ------------------------------------------------------------------
    # Коды /connect
    # ------------------------------------------------------------------
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
    # Журнал действий
    # ------------------------------------------------------------------
    async def add_action_log(
        self, guild_id: Optional[int], action: str, details: str = ""
    ) -> None:
        await self._execute(
            "INSERT INTO action_log (guild_id, action, details, created_at) VALUES (?, ?, ?, ?)",
            (guild_id, action, details, _now()),
        )
