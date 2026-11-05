"""Приватный режим: /unlock и приветствие на новом сервере.

Ког ядра, загружается только при PRIVATE_MODE=true. Сама проверка
«разблокирован ли сервер» — в AinaBot._private_mode_check (одна на все
команды, по множеству в памяти, без обращений к базе).
"""

import hmac
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from bot.core import texts
from bot.discord_bot.helpers import deny_admin, is_admin, respond

log = logging.getLogger(__name__)

# Не больше MAX_ATTEMPTS неверных паролей за ATTEMPT_WINDOW секунд на человека.
MAX_ATTEMPTS = 5
ATTEMPT_WINDOW = 600


class UnlockModal(discord.ui.Modal):
    def __init__(self, cog: "PrivacyCog"):
        super().__init__(title=texts.UNLOCK_TITLE, timeout=300)
        self.cog = cog
        self.password = discord.ui.TextInput(
            label=texts.UNLOCK_FIELD, max_length=200, style=discord.TextStyle.short
        )
        self.add_item(self.password)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.try_unlock(interaction, self.password.value)


class PrivacyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # user_id → моменты неверных попыток (в памяти, только последние).
        self.failures: dict[int, list[float]] = {}

    def _recent_failures(self, user_id: int) -> list[float]:
        now = time.monotonic()
        recent = [t for t in self.failures.get(user_id, []) if now - t < ATTEMPT_WINDOW]
        if recent:
            self.failures[user_id] = recent
        else:
            self.failures.pop(user_id, None)
        return recent

    @app_commands.command(name="unlock", description="Разблокировать бота на сервере паролем (админ)")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def unlock_command(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction):
            await deny_admin(interaction)
            return
        if interaction.guild_id in self.bot.unlocked_guilds:
            await respond(interaction, texts.UNLOCK_ALREADY, colour=texts.EMBED_COLOR_SUCCESS)
            return
        if len(self._recent_failures(interaction.user.id)) >= MAX_ATTEMPTS:
            await respond(interaction, texts.UNLOCK_TOO_MANY.format(minutes=ATTEMPT_WINDOW // 60),
                          colour=texts.EMBED_COLOR_ERROR)
            return
        await interaction.response.send_modal(UnlockModal(self))

    async def try_unlock(self, interaction: discord.Interaction, password: str) -> None:
        user_id = interaction.user.id
        if len(self._recent_failures(user_id)) >= MAX_ATTEMPTS:
            await respond(interaction, texts.UNLOCK_TOO_MANY.format(minutes=ATTEMPT_WINDOW // 60),
                          colour=texts.EMBED_COLOR_ERROR)
            return
        # compare_digest — без утечки по времени сравнения.
        if not hmac.compare_digest(password.encode(), self.bot.config.bot_password.encode()):
            self.failures.setdefault(user_id, []).append(time.monotonic())
            left = MAX_ATTEMPTS - len(self.failures[user_id])
            log.warning("Неверный пароль /unlock: сервер %s, пользователь %s",
                        interaction.guild_id, user_id)
            await respond(interaction, texts.UNLOCK_WRONG.format(left=left),
                          colour=texts.EMBED_COLOR_ERROR)
            return
        self.failures.pop(user_id, None)
        await self.bot.repo.unlock_guild(interaction.guild_id, user_id)
        self.bot.unlocked_guilds.add(interaction.guild_id)
        await self.bot.repo.add_action_log(interaction.guild_id, "unlocked", f"by={user_id}")
        await respond(interaction, texts.UNLOCK_DONE, title=texts.UNLOCK_TITLE,
                      colour=texts.EMBED_COLOR_SUCCESS)

    # ------------------------------------------------------------------
    # Новый сервер: сообщаем, что нужен пароль
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        if guild.id in self.bot.unlocked_guilds:
            return
        embed = discord.Embed(
            title=texts.PRIVATE_WELCOME_TITLE, description=texts.PRIVATE_WELCOME,
            colour=texts.EMBED_COLOR,
        )
        for channel in self._announce_channels(guild):
            try:
                await channel.send(embed=embed)
                return
            except discord.HTTPException:
                continue
        # Писать на сервере некуда — сообщаем владельцу в личные сообщения.
        owner = guild.owner
        if owner is not None:
            try:
                await owner.send(embed=embed)
            except discord.HTTPException:
                log.info("Не удалось сообщить о пароле на сервере %s", guild.id)

    @staticmethod
    def _announce_channels(guild: discord.Guild) -> list[discord.TextChannel]:
        """Системный канал, затем остальные текстовые — куда бот может писать."""
        candidates = [guild.system_channel] if guild.system_channel else []
        candidates += sorted(guild.text_channels, key=lambda c: c.position)
        me = guild.me
        return [
            channel for channel in dict.fromkeys(candidates)
            if channel.permissions_for(me).send_messages and channel.permissions_for(me).embed_links
        ]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PrivacyCog(bot))
