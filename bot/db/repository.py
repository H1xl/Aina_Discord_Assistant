"""Репозиторий ядра: Telegram-связки, коды /connect, журнал действий.

Репозитории модулей наследуются от него, чтобы иметь доступ к общему
журналу действий.
"""

import time
from typing import Optional

import aiosqlite

from .database import BaseRepository


def _now() -> int:
    return int(time.time())


class Repository(BaseRepository):
    # ------------------------------------------------------------------
    # Telegram-связки (многие-ко-многим)
    # ------------------------------------------------------------------
    async def add_telegram_link(
        self, guild_id: int, telegram_chat_id: int, chat_title: Optional[str]
    ) -> bool:
        """Привязывает чат к серверу. Возвращает False, если связка уже была."""
        existed = await self._fetchone(
            "SELECT 1 FROM telegram_links WHERE guild_id = ? AND telegram_chat_id = ?",
            (guild_id, telegram_chat_id),
        )
        await self._execute(
            """
            INSERT INTO telegram_links (guild_id, telegram_chat_id, chat_title, linked_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, telegram_chat_id) DO UPDATE SET
                chat_title = excluded.chat_title
            """,
            (guild_id, telegram_chat_id, chat_title, _now()),
        )
        return existed is None

    async def remove_telegram_link(self, guild_id: int, telegram_chat_id: int) -> bool:
        return bool(await self._execute(
            "DELETE FROM telegram_links WHERE guild_id = ? AND telegram_chat_id = ?",
            (guild_id, telegram_chat_id),
        ))

    async def remove_telegram_chat(self, telegram_chat_id: int) -> None:
        """Удаляет все связки чата (бота выгнали из чата)."""
        await self._execute(
            "DELETE FROM telegram_links WHERE telegram_chat_id = ?", (telegram_chat_id,)
        )

    async def migrate_telegram_chat(self, old_chat_id: int, new_chat_id: int) -> None:
        """Группа превратилась в супергруппу — у неё сменился chat_id."""
        await self._execute(
            "UPDATE OR IGNORE telegram_links SET telegram_chat_id = ? WHERE telegram_chat_id = ?",
            (new_chat_id, old_chat_id),
        )
        await self.remove_telegram_chat(old_chat_id)

    async def get_telegram_links(self, guild_id: int) -> list[aiosqlite.Row]:
        return await self._fetchall(
            "SELECT * FROM telegram_links WHERE guild_id = ? ORDER BY linked_at",
            (guild_id,),
        )

    async def get_telegram_chat_ids(self, guild_id: int) -> list[int]:
        return [row["telegram_chat_id"] for row in await self.get_telegram_links(guild_id)]

    async def get_linked_guild_ids(self, telegram_chat_id: int) -> list[int]:
        rows = await self._fetchall(
            "SELECT guild_id FROM telegram_links WHERE telegram_chat_id = ? ORDER BY linked_at",
            (telegram_chat_id,),
        )
        return [row["guild_id"] for row in rows]

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
    # Модули по серверам
    # ------------------------------------------------------------------
    async def get_guild_modules(self, guild_id: int) -> dict[str, bool]:
        """Явно заданные состояния модулей сервера (остальные — по умолчанию)."""
        rows = await self._fetchall(
            "SELECT module, enabled FROM guild_modules WHERE guild_id = ?", (guild_id,)
        )
        return {row["module"]: bool(row["enabled"]) for row in rows}

    async def set_guild_module(self, guild_id: int, module: str, enabled: bool) -> None:
        await self._execute(
            """
            INSERT INTO guild_modules (guild_id, module, enabled) VALUES (?, ?, ?)
            ON CONFLICT(guild_id, module) DO UPDATE SET enabled = excluded.enabled
            """,
            (guild_id, module, 1 if enabled else 0),
        )

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
