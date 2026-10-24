"""Ephemeral-меню /protect и /unprotect.

Параметры задаются прямо в сообщении (select-меню и кнопки), как в /setup.
Произвольное число минут Discord позволяет ввести только текстом — для
этого кнопка «Своё время…» открывает маленькую модалку с одним полем.
"""

import time
from typing import TYPE_CHECKING, Optional

import discord

from bot.discord_bot.helpers import expire_view_message, respond
from bot.modules.mute_guard import texts
from bot.modules.mute_guard.models import ALL_GUARDS, DEFAULT_GUARDS, serialize_guards

if TYPE_CHECKING:
    from bot.modules.mute_guard.cog import MuteGuardCog

INFINITE = -1
MAX_MINUTES = 525600  # год
DEFAULT_MINUTES = 60
# Готовые варианты длительности, минуты.
DURATION_PRESETS = (15, 30, 60, 180, 360, 720, 1440, 4320, 10080, INFINITE)


def format_minutes(minutes: int) -> str:
    if minutes == INFINITE:
        return texts.DURATION_INFINITE
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days} д.")
    if hours:
        parts.append(f"{hours} ч.")
    if mins:
        parts.append(f"{mins} мин.")
    return " ".join(parts)


def format_until(expires_at: Optional[int]) -> str:
    if expires_at is None:
        return texts.DURATION_INFINITE
    return texts.PROTECT_UNTIL.format(time=f"<t:{expires_at}:f>", relative=f"<t:{expires_at}:R>")


def format_guards(guards) -> str:
    return ", ".join(texts.GUARD_LABELS[g][2] for g in ALL_GUARDS if g in guards)


def parse_minutes(value: str) -> Optional[int]:
    try:
        minutes = int(value.strip())
    except ValueError:
        return None
    if minutes == INFINITE or 1 <= minutes <= MAX_MINUTES:
        return minutes
    return None


class CustomDurationModal(discord.ui.Modal):
    def __init__(self, view: "ProtectView"):
        super().__init__(title=texts.CUSTOM_DURATION_TITLE, timeout=300)
        self.view = view
        self.minutes = discord.ui.TextInput(
            label=texts.CUSTOM_DURATION_LABEL,
            default=str(view.minutes),
            min_length=1,
            max_length=6,
        )
        self.add_item(self.minutes)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        minutes = parse_minutes(self.minutes.value)
        if minutes is None:
            await respond(
                interaction,
                texts.CUSTOM_DURATION_INVALID.format(value=self.minutes.value, max=MAX_MINUTES),
                colour=texts.EMBED_COLOR_ERROR,
            )
            return
        self.view.minutes = minutes
        await self.view.refresh(interaction)


class ProtectView(discord.ui.View):
    def __init__(self, cog: "MuteGuardCog", author_id: int):
        super().__init__(timeout=600)
        self.cog = cog
        self.author_id = author_id
        self.member: Optional[discord.Member] = None
        self.minutes = DEFAULT_MINUTES
        self.notify = True
        self.guards: set[str] = set(DEFAULT_GUARDS)
        self.message: Optional[discord.Message] = None
        self._finished = False
        self._build()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    async def on_timeout(self) -> None:
        if not self._finished:
            await expire_view_message(self.message)

    def _build(self) -> None:
        """Пересобирает компоненты, отражая текущий выбор (после edit_message
        Discord рисует select-меню заново, поэтому выбор задаём как default)."""
        self.clear_items()

        user_select = discord.ui.UserSelect(
            placeholder=texts.PROTECT_USER_PLACEHOLDER,
            default_values=[self.member] if self.member else [],
            row=0,
        )

        async def on_user(interaction: discord.Interaction) -> None:
            picked = user_select.values[0]
            member = interaction.guild.get_member(picked.id)
            if member is None or member.bot:
                await respond(interaction, texts.PROTECT_BOT_USER,
                              colour=texts.EMBED_COLOR_WARNING)
                return
            self.member = member
            await self.refresh(interaction)

        user_select.callback = on_user
        self.add_item(user_select)

        guards_select = discord.ui.Select(
            placeholder=texts.PROTECT_GUARDS_PLACEHOLDER,
            min_values=1,
            max_values=len(ALL_GUARDS),
            options=[
                discord.SelectOption(
                    label=texts.GUARD_LABELS[g][0],
                    description=texts.GUARD_LABELS[g][1],
                    value=g,
                    default=g in self.guards,
                )
                for g in ALL_GUARDS
            ],
            row=1,
        )

        async def on_guards(interaction: discord.Interaction) -> None:
            self.guards = set(guards_select.values)
            await self.refresh(interaction)

        guards_select.callback = on_guards
        self.add_item(guards_select)

        presets = list(DURATION_PRESETS)
        if self.minutes not in presets:
            presets.insert(0, self.minutes)  # своё значение — тоже как пункт меню
        duration_select = discord.ui.Select(
            placeholder=texts.PROTECT_DURATION_PLACEHOLDER,
            options=[
                discord.SelectOption(
                    label=format_minutes(m), value=str(m), default=m == self.minutes
                )
                for m in presets
            ],
            row=2,
        )

        async def on_duration(interaction: discord.Interaction) -> None:
            self.minutes = int(duration_select.values[0])
            await self.refresh(interaction)

        duration_select.callback = on_duration
        self.add_item(duration_select)

        notify_select = discord.ui.Select(
            placeholder=texts.PROTECT_NOTIFY_PLACEHOLDER,
            options=[
                discord.SelectOption(
                    label=texts.PROTECT_NOTIFY_ON, value="1", default=self.notify,
                    description=texts.PROTECT_NOTIFY_ON_HINT.format(
                        minutes=self.cog.config.mute_guard_notify_cooldown // 60
                    ),
                ),
                discord.SelectOption(
                    label=texts.PROTECT_NOTIFY_OFF, value="0", default=not self.notify
                ),
            ],
            row=3,
        )

        async def on_notify(interaction: discord.Interaction) -> None:
            self.notify = notify_select.values[0] == "1"
            await self.refresh(interaction)

        notify_select.callback = on_notify
        self.add_item(notify_select)

        custom = discord.ui.Button(
            label=texts.PROTECT_CUSTOM_BUTTON, style=discord.ButtonStyle.secondary, row=4
        )

        async def on_custom(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(CustomDurationModal(self))

        custom.callback = on_custom
        self.add_item(custom)

        apply = discord.ui.Button(
            label=texts.PROTECT_APPLY_BUTTON, style=discord.ButtonStyle.success, row=4
        )
        apply.callback = self._on_apply
        self.add_item(apply)

    def embed(self) -> discord.Embed:
        return discord.Embed(
            title=texts.PROTECT_TITLE,
            description=texts.PROTECT_PROMPT.format(
                user=self.member.mention if self.member else texts.PROTECT_NOT_CHOSEN,
                guards=format_guards(self.guards),
                duration=format_minutes(self.minutes),
                notify=texts.PROTECT_NOTIFY_ON if self.notify else texts.PROTECT_NOTIFY_OFF,
            ),
            colour=texts.EMBED_COLOR,
        )

    async def refresh(self, interaction: discord.Interaction) -> None:
        self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _on_apply(self, interaction: discord.Interaction) -> None:
        if self.member is None:
            await respond(interaction, texts.PROTECT_NO_USER, colour=texts.EMBED_COLOR_WARNING)
            return
        self._finished = True
        self.stop()
        expires_at = None if self.minutes == INFINITE else int(time.time()) + self.minutes * 60
        await self.cog.apply_protection(
            interaction, self.member, expires_at, self.notify, serialize_guards(self.guards)
        )


class UnprotectView(discord.ui.View):
    def __init__(self, cog: "MuteGuardCog", author_id: int, options: list[discord.SelectOption]):
        super().__init__(timeout=600)
        self.cog = cog
        self.author_id = author_id
        self.selected_ids: list[int] = []
        self.message: Optional[discord.Message] = None
        self._finished = False

        self.select = discord.ui.Select(
            placeholder=texts.UNPROTECT_SELECT,
            min_values=1,
            max_values=len(options),
            options=options,
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

        button = discord.ui.Button(label=texts.UNPROTECT_BUTTON, style=discord.ButtonStyle.danger)
        button.callback = self._on_remove
        self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    async def on_timeout(self) -> None:
        if not self._finished:
            await expire_view_message(self.message)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        self.selected_ids = [int(v) for v in self.select.values]
        await interaction.response.defer()

    async def _on_remove(self, interaction: discord.Interaction) -> None:
        if not self.selected_ids:
            await respond(interaction, texts.UNPROTECT_NOTHING_SELECTED,
                          colour=texts.EMBED_COLOR_WARNING)
            return
        self._finished = True
        self.stop()
        await self.cog.remove_protections(interaction, self.selected_ids)
