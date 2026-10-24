"""Модуль «Роли»: верификация новичков, именные роли, импорт существующих ролей."""

from bot.modules import ModuleSpec

from .models import MIGRATIONS, NAMESPACE

MODULE = ModuleSpec(
    name=NAMESPACE,
    title="Автоматическая выдача ролей",
    description="встречаю новичков, оформляю именные роли, возвращаю роли вернувшимся",
    extensions=(
        "bot.modules.roles.cogs.verification",
        "bot.modules.roles.cogs.setup",
        "bot.modules.roles.cogs.import_roles",
    ),
    migrations=MIGRATIONS,
    telegram_routers=("bot.modules.roles.telegram",),
)
