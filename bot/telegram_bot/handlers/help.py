"""/help в Telegram: кто я, команды и модули.

Список команд и модулей собирается из загруженных модулей, поэтому новые
модули попадают в справку без правок здесь.
"""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.core import texts
from bot.telegram_bot.notifier import tg_escape

router = Router(name="help")


def render_help(discord_bot) -> str:
    # Импорт здесь: bot.telegram_bot.bot подключает этот роутер.
    from bot.telegram_bot.bot import all_commands

    commands = "\n".join(
        texts.TG_HELP_COMMAND.format(command=command, description=tg_escape(description))
        for command, description in all_commands(discord_bot)
    )
    modules = "\n".join(
        texts.TG_HELP_MODULE.format(
            title=tg_escape(module.title), description=tg_escape(module.description)
        )
        for module in discord_bot.modules
    )
    return texts.TG_HELP.format(commands=commands, modules=modules)


@router.message(Command("help"))
async def cmd_help(message: Message, discord_bot) -> None:
    await message.answer(render_help(discord_bot), parse_mode="HTML")
