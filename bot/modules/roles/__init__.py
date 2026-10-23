"""Модуль «Роли»: верификация новичков, именные роли, импорт существующих ролей."""

from bot.modules import ModuleSpec

from .models import MIGRATIONS, NAMESPACE

MODULE = ModuleSpec(
    name=NAMESPACE,
    title="Автоматическая выдача ролей",
    extensions=(
        "bot.modules.roles.cogs.verification",
        "bot.modules.roles.cogs.setup",
    ),
    migrations=MIGRATIONS,
)
