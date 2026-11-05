"""Текстовые шаблоны модуля сводки голосовых каналов (Telegram, HTML)."""

from bot.core.texts import *  # noqa: F401,F403

# Шапка сводки одного сервера (у каждого сервера — своё сообщение).
DASHBOARD_TITLE = "🏰 <b>{guild_name}</b> · 🎧 голосовые каналы"
DASHBOARD_CHANNEL = "🔊 <b>{channel}</b> · {count}"
DASHBOARD_MEMBER = "    • {member} — {duration}{states}"
DASHBOARD_EMPTY = "<i>в голосовых каналах никого нет</i>"
DASHBOARD_TRUNCATED = "<i>… и ещё — сводка не поместилась целиком</i>"
DASHBOARD_LEGEND = (
    "<i>🙊 сам(а) выключил(а) микрофон · 🔇 микрофон отключён администратором\n"
    "🙉 сам(а) выключил(а) звук · 🔕 звук отключён администратором\n"
    "📺 стрим · 📷 камера</i>"
)

# Длительность пребывания в канале.
DURATION_LESS_MINUTE = "&lt;1 мин"
DURATION_MINUTES = "{minutes} мин"
DURATION_HOURS = "{hours} ч {minutes:02d} мин"
# Время входа неизвестно (сидел ещё до запуска бота) — «не меньше».
DURATION_AT_LEAST = "≥ {duration}"

# Ответы на команды.
DASHBOARD_NO_SERVERS = (
    "Прошу прощения, но этот чат не привязан ни к одному серверу, где включена "
    "сводка голосовых каналов. Попросите администратора Discord выполнить /connect "
    "и пришлите мне код."
)
DASHBOARD_NOT_FOUND = "Сводки голосовых каналов в этом чате сейчас нет."
DASHBOARD_OFF_FORBIDDEN = "Прошу прощения, убирать сводку могут только администраторы чата."
