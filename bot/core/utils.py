"""Мелкие утилиты, общие для Discord- и Telegram-части."""

import re

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def parse_hex_color(value: str) -> int | None:
    """Разбирает строку вида ``#RRGGBB`` или ``RRGGBB`` в целое число цвета.

    Возвращает None, если формат неверный.
    """
    match = _HEX_RE.match(value.strip())
    if not match:
        return None
    return int(match.group(1), 16)


def format_hex_color(value: int) -> str:
    return f"#{value:06X}"
