"""Подключение к SQLite и версионные миграции схемы.

Каждый модуль (и ядро) ведёт свою цепочку миграций под собственным
пространством имён в таблице ``schema_versions``. Миграция — список
SQL-выражений; её номер — позиция в списке (с 1). Выражения пишутся
идемпотентно (IF NOT EXISTS, INSERT OR IGNORE), поэтому повторный прогон
после сбоя посередине безопасен.
"""

import logging
import sqlite3
from pathlib import Path
from typing import Optional, Sequence

import aiosqlite

log = logging.getLogger(__name__)

Migration = Sequence[str]


class Database:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    @classmethod
    async def connect(cls, db_path: str) -> "Database":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_versions (
                namespace TEXT PRIMARY KEY,
                version   INTEGER NOT NULL
            )
            """
        )
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    async def _get_version(self, namespace: str) -> int:
        async with self.conn.execute(
            "SELECT version FROM schema_versions WHERE namespace = ?", (namespace,)
        ) as cur:
            row: Optional[aiosqlite.Row] = await cur.fetchone()
        return row["version"] if row else 0

    async def migrate(self, namespace: str, migrations: Sequence[Migration]) -> None:
        """Применяет недостающие миграции пространства имён по порядку."""
        current = await self._get_version(namespace)
        for version, statements in enumerate(migrations, start=1):
            if version <= current:
                continue
            for statement in statements:
                try:
                    await self.conn.execute(statement)
                except sqlite3.OperationalError as exc:
                    # ALTER TABLE ADD COLUMN не бывает «IF NOT EXISTS» —
                    # при повторном прогоне колонка уже есть, это не ошибка.
                    if "duplicate column name" not in str(exc):
                        raise
            await self.conn.execute(
                """
                INSERT INTO schema_versions (namespace, version) VALUES (?, ?)
                ON CONFLICT(namespace) DO UPDATE SET version = excluded.version
                """,
                (namespace, version),
            )
            await self.conn.commit()
            log.info("Миграция %s → v%d применена", namespace, version)


class BaseRepository:
    """Общие хелперы запросов для репозиториев ядра и модулей."""

    def __init__(self, db: Database):
        self._db = db
        self._conn = db.conn

    async def _fetchone(self, sql: str, params: tuple = ()) -> Optional[aiosqlite.Row]:
        async with self._conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def _fetchall(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        async with self._conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def _execute(self, sql: str, params: tuple = ()) -> int:
        """Выполняет запрос с коммитом; возвращает число затронутых строк."""
        async with self._conn.execute(sql, params) as cur:
            rowcount = cur.rowcount
        await self._conn.commit()
        return rowcount
