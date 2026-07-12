"""Select-меню: выбор опциональных ролей при верификации и View для /setup."""

from typing import TYPE_CHECKING, Optional

import discord

from bot.core import texts

if TYPE_CHECKING:
    from bot.discord_bot.cogs.verification import VerificationCog


class OptionalRolesView(discord.ui.View):
    """Ephemeral-меню «Выберите дополнительные роли» + кнопка «Готово»."""

    # Discord-компоненты «протухают» через ~15 минут неактивности —
    # держим таймаут чуть меньше и подсказываем вызвать /role заново.
    def __init__(
        self,
        cog: "VerificationCog",
        roles: list[discord.Role],
        role_name: str,
        color_value: int,
        inviter: Optional[str],
        tracked: bool,
    ):
        super().__init__(timeout=840)
        self.cog = cog
        self.role_name = role_name
        self.color_value = color_value
        self.inviter = inviter
        self.tracked = tracked
        self.selected_ids: list[int] = []
        self.message: Optional[discord.Message] = None
        self._finished = False

        options = [
            discord.SelectOption(label=role.name[:100], value=str(role.id))
            for role in roles[:25]
        ]
        self.select = discord.ui.Select(
            placeholder="Дополнительные роли (необязательно)",
            min_values=0,
            max_values=len(options),
            options=options,
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

        done = discord.ui.Button(
            label=texts.OPTIONAL_ROLES_DONE_BUTTON,
            style=discord.ButtonStyle.success,
        )
        done.callback = self._on_done
        self.add_item(done)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        self.selected_ids = [int(v) for v in self.select.values]
        await interaction.response.defer()

    async def _on_done(self, interaction: discord.Interaction) -> None:
        self._finished = True
        self.stop()
        # Финальный шаг долгий — сразу deferred-ответ, иначе interaction
        # протухнет (404 Unknown interaction); заодно гасим само меню.
        await interaction.response.defer(ephemeral=True, thinking=True)
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass
        await self.cog.finalize_verification(
            interaction,
            role_name=self.role_name,
            color_value=self.color_value,
            inviter=self.inviter,
            optional_role_ids=self.selected_ids,
            tracked=self.tracked,
        )

    async def on_timeout(self) -> None:
        if self._finished or self.message is None:
            return
        try:
            await self.message.edit(
                embed=discord.Embed(
                    description=texts.OPTIONAL_ROLES_EXPIRED,
                    colour=texts.EMBED_COLOR_WARNING,
                ),
                view=None,
            )
        except discord.HTTPException:
            pass


class SetupChannelsView(discord.ui.View):
    """Шаг 1 /setup: три ChannelSelect + кнопка «Далее».

    В одном View помещается максимум 5 строк компонентов, поэтому /setup
    разбит на два шага: каналы → роли (обязательные + «Не имеет ролей»).
    """

    def __init__(self, author_id: int):
        super().__init__(timeout=600)
        self.author_id = author_id
        self.roles_channel_id: Optional[int] = None
        self.pings_channel_id: Optional[int] = None
        self.log_channel_id: Optional[int] = None
        self.finished = False
        self.message: Optional[discord.Message] = None
        self._next_callback = None  # проставляется когом

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        placeholder="Канал [роли] — приглашения новичков",
        row=0,
    )
    async def roles_channel(
        self, interaction: discord.Interaction, select: discord.ui.ChannelSelect
    ) -> None:
        self.roles_channel_id = select.values[0].id
        await interaction.response.defer()

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        placeholder="Канал [пинги] — поздравления",
        row=1,
    )
    async def pings_channel(
        self, interaction: discord.Interaction, select: discord.ui.ChannelSelect
    ) -> None:
        self.pings_channel_id = select.values[0].id
        await interaction.response.defer()

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        placeholder="Канал [журнал] — лог событий",
        row=2,
    )
    async def log_channel(
        self, interaction: discord.Interaction, select: discord.ui.ChannelSelect
    ) -> None:
        self.log_channel_id = select.values[0].id
        await interaction.response.defer()

    @discord.ui.button(
        label=texts.SETUP_NEXT_BUTTON, style=discord.ButtonStyle.primary, row=3
    )
    async def next_step(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not (self.roles_channel_id and self.pings_channel_id and self.log_channel_id):
            await interaction.response.send_message(
                embed=discord.Embed(
                    description=texts.SETUP_INCOMPLETE,
                    colour=texts.EMBED_COLOR_WARNING,
                ),
                ephemeral=True,
            )
            return
        self.finished = True
        self.stop()
        if self._next_callback is not None:
            await self._next_callback(interaction, self)

    async def on_timeout(self) -> None:
        if self.finished or self.message is None:
            return
        try:
            await self.message.edit(
                embed=discord.Embed(
                    description=texts.SETUP_EXPIRED,
                    colour=texts.EMBED_COLOR_WARNING,
                ),
                view=None,
            )
        except discord.HTTPException:
            pass


class SetupRolesView(discord.ui.View):
    """Шаг 2 /setup: обязательные роли + роль «Не имеет ролей» + «Сохранить»."""

    def __init__(self, channels_view: SetupChannelsView):
        super().__init__(timeout=600)
        self.author_id = channels_view.author_id
        # Данные шага 1 переносим сюда — ког сохраняет всё из этого view.
        self.roles_channel_id = channels_view.roles_channel_id
        self.pings_channel_id = channels_view.pings_channel_id
        self.log_channel_id = channels_view.log_channel_id
        self.mandatory_role_ids: list[int] = []
        # Роль «Не имеет ролей», выбранная админом; None — найти/создать
        # автоматически, как раньше.
        self.no_role_pick_id: Optional[int] = None
        self.saved = False
        self.message: Optional[discord.Message] = channels_view.message
        self._save_callback = None  # проставляется когом

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="Обязательные роли для новичков",
        min_values=0,
        max_values=25,
        row=0,
    )
    async def mandatory_roles(
        self, interaction: discord.Interaction, select: discord.ui.RoleSelect
    ) -> None:
        self.mandatory_role_ids = [role.id for role in select.values]
        await interaction.response.defer()

    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="Роль «Не имеет ролей» (пусто — создам сама)",
        min_values=0,
        max_values=1,
        row=1,
    )
    async def no_role_pick(
        self, interaction: discord.Interaction, select: discord.ui.RoleSelect
    ) -> None:
        self.no_role_pick_id = select.values[0].id if select.values else None
        await interaction.response.defer()

    @discord.ui.button(
        label=texts.SETUP_SAVE_BUTTON, style=discord.ButtonStyle.success, row=2
    )
    async def save(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.saved = True
        self.stop()
        if self._save_callback is not None:
            await self._save_callback(interaction, self)

    async def on_timeout(self) -> None:
        if self.saved or self.message is None:
            return
        try:
            await self.message.edit(
                embed=discord.Embed(
                    description=texts.SETUP_EXPIRED,
                    colour=texts.EMBED_COLOR_WARNING,
                ),
                view=None,
            )
        except discord.HTTPException:
            pass


class OptionalRolesSetupView(discord.ui.View):
    """View для /setup-optional-roles: RoleSelect пула + кнопка «Сохранить»."""

    def __init__(self, author_id: int):
        super().__init__(timeout=600)
        self.author_id = author_id
        self.role_ids: list[int] = []
        self.saved = False
        self.message: Optional[discord.Message] = None
        self._save_callback = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="Пул дополнительных ролей (до 25)",
        min_values=0,
        max_values=25,
        row=0,
    )
    async def pool(
        self, interaction: discord.Interaction, select: discord.ui.RoleSelect
    ) -> None:
        self.role_ids = [role.id for role in select.values]
        await interaction.response.defer()

    @discord.ui.button(
        label=texts.SETUP_SAVE_BUTTON, style=discord.ButtonStyle.success, row=1
    )
    async def save(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.saved = True
        self.stop()
        if self._save_callback is not None:
            await self._save_callback(interaction, self)

    async def on_timeout(self) -> None:
        if self.saved or self.message is None:
            return
        try:
            await self.message.edit(
                embed=discord.Embed(
                    description=texts.SETUP_EXPIRED,
                    colour=texts.EMBED_COLOR_WARNING,
                ),
                view=None,
            )
        except discord.HTTPException:
            pass
