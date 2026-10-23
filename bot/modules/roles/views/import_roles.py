"""Пошаговое меню /import-roles: выбор ролей → сводка → разбор спорных → сохранение.

Все шаги живут в одном ephemeral-сообщении: View перестраивает свои
компоненты под текущий шаг (выбор ролей / сводка / разбор роли).
"""

import difflib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

import discord

from bot.discord_bot.helpers import expire_view_message, respond
from bot.modules.roles import texts

if TYPE_CHECKING:
    from bot.modules.roles.cogs.import_roles import ImportRolesCog

# Сколько владельцев перечислять в строке сводки.
_SUMMARY_MEMBERS_LIMIT = 5
# Discord ограничивает описание embed 4096 символами.
_EMBED_LIMIT = 4000

# Статусы роли после анализа.
INVALID = "invalid"      # не может быть именной (служебная, выше бота, из настроек)
ALREADY = "already"      # уже привязана к текущему носителю
AUTO = "auto"            # единственный носитель — привязываем к нему
SUGGESTED = "suggested"  # несколько носителей, но один похож по имени
AMBIGUOUS = "ambiguous"  # несколько носителей — нужен выбор админа
NOBODY = "nobody"        # носителей нет — админ может указать владельца

# Как был определён владелец.
OWNER_AUTO = "auto"
OWNER_SUGGESTED = "suggested"
OWNER_CHOSEN = "chosen"


@dataclass
class RoleEntry:
    role: discord.Role
    holders: list[discord.Member]
    status: str
    reason: Optional[str] = None
    owner: Optional[discord.Member] = None
    owner_source: Optional[str] = None
    skipped: bool = False

    @property
    def needs_review(self) -> bool:
        return self.status in (SUGGESTED, AMBIGUOUS, NOBODY)

    @property
    def grants_role(self) -> bool:
        """Владелец выбран вручную и роли у него ещё нет — её нужно выдать."""
        return self.owner is not None and self.owner not in self.holders


def _normalize(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold())


def suggest_owner(role_name: str, holders: list[discord.Member]) -> Optional[discord.Member]:
    """Ищет носителя, чьё имя явно похоже на название роли.

    Именные роли обычно называют по нику владельца — сравниваем название
    с display name, глобальным именем и username. Возвращаем кандидата,
    только если он заметно лучше остальных.
    """
    target = _normalize(role_name)
    if not target:
        return None

    def score(member: discord.Member) -> float:
        best = 0.0
        for name in (member.display_name, member.global_name, member.name):
            candidate = _normalize(name or "")
            if not candidate:
                continue
            if target in candidate or candidate in target:
                best = max(best, 0.9)
            best = max(best, difflib.SequenceMatcher(None, target, candidate).ratio())
        return best

    ranked = sorted(((score(m), m) for m in holders), key=lambda pair: pair[0], reverse=True)
    if not ranked or ranked[0][0] < 0.6:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.15:
        return None
    return ranked[0][1]


def analyze_roles(
    guild: discord.Guild,
    roles: list[discord.Role],
    bound: dict[int, int],
    reserved_ids: set[int],
) -> list[RoleEntry]:
    """Сопоставляет выбранные роли с их носителями.

    ``bound`` — текущие привязки из БД (role_id → user_id),
    ``reserved_ids`` — роли из настроек верификации.
    """
    entries = []
    top = guild.me.top_role
    for role in sorted(roles, key=lambda r: r.position, reverse=True):
        holders = [m for m in role.members if not m.bot]
        if role.is_default():
            entries.append(RoleEntry(role, holders, INVALID, texts.IMPORT_REASON_DEFAULT))
            continue
        if role.managed:
            entries.append(RoleEntry(role, holders, INVALID, texts.IMPORT_REASON_MANAGED))
            continue
        if role >= top:
            entries.append(RoleEntry(role, holders, INVALID, texts.IMPORT_REASON_ABOVE_BOT))
            continue
        if role.id in reserved_ids:
            entries.append(RoleEntry(role, holders, INVALID, texts.IMPORT_REASON_SETTINGS))
            continue

        current = next((m for m in holders if m.id == bound.get(role.id)), None)
        if current is not None:
            entries.append(RoleEntry(role, holders, ALREADY, owner=current))
        elif len(holders) == 1:
            entries.append(RoleEntry(role, holders, AUTO, owner=holders[0],
                                     owner_source=OWNER_AUTO))
        elif not holders:
            entries.append(RoleEntry(role, holders, NOBODY))
        elif (guess := suggest_owner(role.name, holders)) is not None:
            entries.append(RoleEntry(role, holders, SUGGESTED, owner=guess,
                                     owner_source=OWNER_SUGGESTED))
        else:
            entries.append(RoleEntry(role, holders, AMBIGUOUS))
    return entries


def build_plan(entries: list[RoleEntry]) -> tuple[list[RoleEntry], set[int]]:
    """Итоговый список привязок и id ролей, отброшенных из-за дубля владельца.

    У участника может быть только одна именная роль, поэтому при выборе
    одного человека для нескольких ролей привязывается первая (верхняя).
    """
    plan, duplicates = [], set()
    # Владельцы уже привязанных ролей из этой же выборки тоже заняты.
    used = {e.owner.id for e in entries if e.status == ALREADY}
    for entry in entries:
        if entry.status in (INVALID, ALREADY) or entry.skipped or entry.owner is None:
            continue
        if entry.owner.id in used:
            duplicates.add(entry.role.id)
            continue
        used.add(entry.owner.id)
        plan.append(entry)
    return plan, duplicates


def _members_preview(members: list[discord.Member]) -> str:
    shown = ", ".join(m.mention for m in members[:_SUMMARY_MEMBERS_LIMIT])
    rest = len(members) - _SUMMARY_MEMBERS_LIMIT
    return f"{shown} и ещё {rest}" if rest > 0 else shown


def render_line(
    entry: RoleEntry, duplicates: set[int], user_roles: dict[int, int], guild: discord.Guild
) -> str:
    role = entry.role.mention
    if entry.status == INVALID:
        return texts.IMPORT_LINE_INVALID.format(role=role, reason=entry.reason)
    if entry.status == ALREADY:
        return texts.IMPORT_LINE_ALREADY.format(role=role, member=entry.owner.mention)
    if entry.skipped:
        return texts.IMPORT_LINE_SKIPPED.format(role=role)
    if entry.owner is None:
        if entry.status == NOBODY:
            return texts.IMPORT_LINE_NO_HOLDERS.format(role=role)
        return texts.IMPORT_LINE_AMBIGUOUS.format(
            role=role, count=len(entry.holders), members=_members_preview(entry.holders)
        )

    member = entry.owner.mention
    if entry.role.id in duplicates:
        return texts.IMPORT_LINE_DUPLICATE.format(role=role, member=member)
    if entry.owner_source == OWNER_AUTO:
        line = texts.IMPORT_LINE_AUTO.format(role=role, member=member)
    elif entry.owner_source == OWNER_SUGGESTED:
        line = texts.IMPORT_LINE_SUGGESTED.format(
            role=role, member=member, count=len(entry.holders)
        )
    elif entry.grants_role:
        line = texts.IMPORT_LINE_GRANT.format(role=role, member=member)
    else:
        line = texts.IMPORT_LINE_CHOSEN.format(role=role, member=member)

    old_role_id = user_roles.get(entry.owner.id)
    if old_role_id and old_role_id != entry.role.id:
        old = guild.get_role(old_role_id)
        line += texts.IMPORT_NOTE_REPLACES.format(old=old.name if old else f"ID {old_role_id}")
    return line


def _truncate(text: str) -> str:
    return text if len(text) <= _EMBED_LIMIT else text[: _EMBED_LIMIT - 1] + "…"


class ImportRolesView(discord.ui.View):
    def __init__(self, cog: "ImportRolesCog", author_id: int):
        super().__init__(timeout=840)
        self.cog = cog
        self.author_id = author_id
        self.message: Optional[discord.Message] = None
        self.picked_roles: list[discord.Role] = []
        self.entries: list[RoleEntry] = []
        # Данные БД на момент анализа — для пометок «заменит прежнюю».
        self.user_roles: dict[int, int] = {}
        self.review_queue: list[RoleEntry] = []
        self.review_index = 0
        self._finished = False
        self._show_pick()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    async def on_timeout(self) -> None:
        if not self._finished:
            await expire_view_message(self.message)

    def _button(self, label: str, style: discord.ButtonStyle, callback, row: int = 1):
        button = discord.ui.Button(label=label, style=style, row=row)
        button.callback = callback
        self.add_item(button)

    # ------------------------------------------------------------------
    # Шаг 1: выбор ролей
    # ------------------------------------------------------------------
    def _show_pick(self) -> None:
        self.clear_items()
        select = discord.ui.RoleSelect(
            placeholder=texts.IMPORT_SELECT, min_values=1, max_values=25, row=0
        )

        async def on_pick(interaction: discord.Interaction) -> None:
            self.picked_roles = list(select.values)
            await interaction.response.defer()

        select.callback = on_pick
        self.add_item(select)
        self._button(texts.IMPORT_ANALYZE_BUTTON, discord.ButtonStyle.primary, self._on_analyze)

    def pick_embed(self) -> discord.Embed:
        return discord.Embed(
            title=texts.IMPORT_TITLE, description=texts.IMPORT_PROMPT, colour=texts.EMBED_COLOR
        )

    async def _on_analyze(self, interaction: discord.Interaction) -> None:
        if not self.picked_roles:
            await respond(interaction, texts.IMPORT_NOTHING_SELECTED,
                          colour=texts.EMBED_COLOR_WARNING)
            return
        self.entries, self.user_roles = await self.cog.analyze(
            interaction.guild, self.picked_roles
        )
        self.review_queue = [e for e in self.entries if e.needs_review]
        await self._edit_summary(interaction)

    # ------------------------------------------------------------------
    # Шаг 2: сводка
    # ------------------------------------------------------------------
    async def _edit_summary(self, interaction: discord.Interaction) -> None:
        self.clear_items()
        if self.review_queue:
            self._button(
                texts.IMPORT_RESOLVE_BUTTON.format(count=len(self.review_queue)),
                discord.ButtonStyle.primary, self._on_start_review, row=0,
            )
        self._button(texts.IMPORT_SAVE_BUTTON, discord.ButtonStyle.success, self._on_save, row=0)
        self._button(texts.IMPORT_CANCEL_BUTTON, discord.ButtonStyle.secondary,
                     self._on_cancel, row=0)
        await interaction.response.edit_message(embed=self.summary_embed(interaction.guild),
                                                view=self)

    def summary_embed(self, guild: discord.Guild) -> discord.Embed:
        _, duplicates = build_plan(self.entries)
        lines = [render_line(e, duplicates, self.user_roles, guild) for e in self.entries]
        description = texts.IMPORT_SUMMARY_HEAD + "\n\n" + "\n".join(lines)
        unresolved = sum(
            1 for e in self.entries if e.needs_review and e.owner is None and not e.skipped
        )
        if unresolved:
            description += texts.IMPORT_SUMMARY_UNRESOLVED.format(count=unresolved)
        return discord.Embed(
            title=texts.IMPORT_TITLE, description=_truncate(description), colour=texts.EMBED_COLOR
        )

    async def _on_start_review(self, interaction: discord.Interaction) -> None:
        self.review_index = 0
        await self._edit_review(interaction)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        self._finished = True
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(description=texts.IMPORT_CANCELLED, colour=texts.EMBED_COLOR),
            view=None,
        )

    async def _on_save(self, interaction: discord.Interaction) -> None:
        plan, _ = build_plan(self.entries)
        if not plan:
            await respond(interaction, texts.IMPORT_NOTHING_TO_SAVE,
                          colour=texts.EMBED_COLOR_WARNING)
            return
        self._finished = True
        self.stop()
        await self.cog.save(interaction, plan)

    # ------------------------------------------------------------------
    # Шаг 3: разбор спорных ролей по одной
    # ------------------------------------------------------------------
    async def _edit_review(self, interaction: discord.Interaction) -> None:
        if self.review_index >= len(self.review_queue):
            await self._edit_summary(interaction)
            return

        entry = self.review_queue[self.review_index]
        self.clear_items()
        holders = entry.holders
        if 0 < len(holders) <= 25:
            options = [
                discord.SelectOption(
                    label=member.display_name[:100],
                    description=(
                        f"@{member.name}"
                        + (f" · {texts.IMPORT_SUGGESTED_HINT}"
                           if entry.owner_source == OWNER_SUGGESTED and member == entry.owner
                           else "")
                    )[:100],
                    value=str(member.id),
                    default=member == entry.owner,
                )
                for member in holders
            ]
            select = discord.ui.Select(
                placeholder=texts.IMPORT_RESOLVE_PLACEHOLDER, options=options, row=0
            )
            description = texts.IMPORT_RESOLVE_HOLDERS.format(
                role=entry.role.mention, count=len(holders),
                members="\n".join(f"• {m.mention} (`{m.name}`)" for m in holders),
            )
        else:
            select = discord.ui.UserSelect(
                placeholder=texts.IMPORT_RESOLVE_PLACEHOLDER,
                default_values=[entry.owner] if entry.owner else [],
                row=0,
            )
            template = texts.IMPORT_RESOLVE_NOBODY if not holders else texts.IMPORT_RESOLVE_MANY
            description = template.format(role=entry.role.mention, count=len(holders))

        async def on_choose(choice_interaction: discord.Interaction) -> None:
            value = select.values[0]
            member_id = int(value) if isinstance(value, str) else value.id
            member = choice_interaction.guild.get_member(member_id)
            if member is not None:
                entry.owner = member
                entry.owner_source = OWNER_CHOSEN
                entry.skipped = False
            self.review_index += 1
            await self._edit_review(choice_interaction)

        async def on_skip(skip_interaction: discord.Interaction) -> None:
            entry.owner = None
            entry.owner_source = None
            entry.skipped = True
            self.review_index += 1
            await self._edit_review(skip_interaction)

        async def on_keep(keep_interaction: discord.Interaction) -> None:
            self.review_index += 1
            await self._edit_review(keep_interaction)

        select.callback = on_choose
        self.add_item(select)
        # Повторный выбор уже выбранного пункта Discord не присылает —
        # поэтому предложенного владельца подтверждаем отдельной кнопкой.
        if entry.owner is not None:
            self._button(texts.IMPORT_KEEP_BUTTON, discord.ButtonStyle.success, on_keep)
        self._button(texts.IMPORT_SKIP_BUTTON, discord.ButtonStyle.secondary, on_skip)
        self._button(texts.IMPORT_BACK_BUTTON, discord.ButtonStyle.secondary, self._edit_summary)

        embed = discord.Embed(
            title=texts.IMPORT_RESOLVE_TITLE.format(
                index=self.review_index + 1, total=len(self.review_queue)
            ),
            description=_truncate(description),
            colour=texts.EMBED_COLOR,
        )
        await interaction.response.edit_message(embed=embed, view=self)
