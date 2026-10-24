"""Реестр модулей бота.

Модуль — пакет в ``bot/modules/`` с объектом ``MODULE`` (ModuleSpec):
собственные миграции БД, Discord-расширения (коги) и, при необходимости,
Telegram-роутеры. Ядро (БД, привязка Telegram, уведомления) загружается
всегда; какие модули загружены в процесс — задаёт ``ENABLED_MODULES`` в .env,
а включены ли они на конкретном сервере — команда ``/modules``
(по умолчанию — ``default_enabled``).

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
    # Пути модулей с объектом ``router`` (aiogram.Router) для Telegram.
    telegram_routers: Sequence[str] = ()
    # Telegram-команды модуля (команда, описание) — для меню бота и /help.
    telegram_commands: Sequence[tuple[str, str]] = ()
    # Включён ли модуль на сервере, пока админ не переключил его в /modules.
    default_enabled: bool = True
    # Короткое описание для меню /modules.
    description: str = ""


# Имя модуля → пакет. Порядок важен только для порядка загрузки.
_REGISTRY = {
    "roles": "bot.modules.roles",
    "mute_guard": "bot.modules.mute_guard",
    "voice_notify": "bot.modules.voice_notify",
    "voice_dashboard": "bot.modules.voice_dashboard",
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
