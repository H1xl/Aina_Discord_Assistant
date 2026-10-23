"""Реестр модулей бота.

Модуль — пакет в ``bot/modules/`` с объектом ``MODULE`` (ModuleSpec):
собственные миграции БД и список Discord-расширений (когов). Ядро
(БД, привязка Telegram, уведомления) загружается всегда, модули —
согласно ``ENABLED_MODULES`` в .env.

Чтобы добавить модуль: создайте пакет с ``MODULE`` и впишите его в ``_REGISTRY``.
"""

import importlib
from dataclasses import dataclass, field
from typing import Sequence

from bot.db.database import Migration


@dataclass(frozen=True)
class ModuleSpec:
    name: str
    title: str
    # Discord-расширения (пути модулей с функцией setup(bot)).
    extensions: Sequence[str]
    migrations: Sequence[Migration] = field(default_factory=list)


# Имя модуля → пакет. Порядок важен только для порядка загрузки.
_REGISTRY = {
    "roles": "bot.modules.roles",
    "mute_guard": "bot.modules.mute_guard",
}

ALL_MODULES = tuple(_REGISTRY)


def load_modules(names: Sequence[str]) -> list[ModuleSpec]:
    unknown = [name for name in names if name not in _REGISTRY]
    if unknown:
        raise RuntimeError(
            f"Неизвестные модули в ENABLED_MODULES: {', '.join(unknown)}. "
            f"Доступны: {', '.join(ALL_MODULES)}"
        )
    return [importlib.import_module(_REGISTRY[name]).MODULE for name in names]
