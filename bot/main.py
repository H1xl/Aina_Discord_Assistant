"""Точка входа: Discord- и Telegram-боты в одном процессе, одна БД.

Ядро (БД, привязка Telegram) поднимается здесь; модули подключает AinaBot
согласно ENABLED_MODULES.

Запуск: python -m bot.main
"""

import asyncio
import logging

from bot.core.config import load_config
from bot.db.database import Database
from bot.db.models import CORE_MIGRATIONS, CORE_NAMESPACE
from bot.db.repository import Repository
from bot.discord_bot.bot import AinaBot
from bot.telegram_bot.bot import create_dispatcher, create_telegram_bot

log = logging.getLogger("main")


async def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = load_config()
    db = await Database.connect(config.db_path)
    await db.migrate(CORE_NAMESPACE, CORE_MIGRATIONS)
    repo = Repository(db)

    discord_bot = AinaBot(config, db, repo)
    tg_bot = create_telegram_bot(config)
    discord_bot.tg_bot = tg_bot
    dispatcher = create_dispatcher(repo, discord_bot)

    log.info("Запускаю Discord- и Telegram-ботов…")
    try:
        await asyncio.gather(
            discord_bot.start(config.discord_token),
            dispatcher.start_polling(tg_bot, handle_signals=False),
        )
    finally:
        log.info("Останавливаюсь…")
        if not discord_bot.is_closed():
            await discord_bot.close()
        await tg_bot.session.close()
        await db.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
