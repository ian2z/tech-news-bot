from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot.pipeline import AllSourcesFailedError

if TYPE_CHECKING:
    from bot.main import TechNewsBot
    from bot.scheduler import NewsSchedulerCog

logger = logging.getLogger(__name__)


class NewsCommandsCog(commands.Cog):
    def __init__(self, bot: TechNewsBot) -> None:
        self.bot = bot

    def _get_scheduler(self) -> NewsSchedulerCog | None:
        cog = self.bot.get_cog("NewsSchedulerCog")
        return cog  # type: ignore[return-value]

    @app_commands.command(
        name="noticias",
        description="Coleta e posta agora o briefing de noticias no canal configurado.",
    )
    @app_commands.describe(
        forcar="Regenerar e publicar mesmo se o briefing de hoje ja tiver sido enviado (respeita cooldown).",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def noticias(self, interaction: discord.Interaction, forcar: bool = False) -> None:
        await interaction.response.defer(ephemeral=True)
        scheduler = self._get_scheduler()
        if scheduler is None:
            await interaction.followup.send("Agendador nao inicializado.", ephemeral=True)
            return

        try:
            count, message = await scheduler.run_and_post(manual=True, force=forcar)
        except AllSourcesFailedError:
            await interaction.followup.send(
                "Falha ao coletar noticias: todas as fontes retornaram erro.",
                ephemeral=True,
            )
            return
        except Exception as exc:
            logger.exception("Erro ao executar /noticias: %s", exc)
            await interaction.followup.send(
                "Ocorreu um erro interno ao gerar o briefing de noticias.",
                ephemeral=True,
            )
            return

        if count == 0:
            if "Nenhuma noticia" in message:
                await interaction.followup.send(
                    "Nao ha novidades para postar no momento.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.followup.send(
                f"Briefing publicado: {message}",
                ephemeral=True,
            )

    @noticias.error
    async def noticias_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            msg = "Voce precisa da permissao Gerenciar Servidor para usar este comando."
        else:
            logger.exception("Erro no comando /noticias: %s", error)
            msg = "Nao foi possivel executar o comando /noticias."

        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(
        name="status",
        description="Mostra o status do bot de noticias.",
    )
    async def status(self, interaction: discord.Interaction) -> None:
        scheduler = self._get_scheduler()
        tz = self.bot.config.timezone

        next_run_str = f"{self.bot.config.post_time_str} ({self.bot.config.timezone_name})"
        if scheduler and scheduler.daily_news.next_iteration:
            next_local = scheduler.daily_news.next_iteration.astimezone(tz)
            next_run_str = next_local.strftime("%d/%m/%Y %H:%M") + f" ({self.bot.config.timezone_name})"

        latest = await self.bot.dedup_store.get_latest_digest()

        if latest is not None:
            gen_local = latest.created_at.astimezone(tz).strftime("%d/%m/%Y %H:%M")
            mode_used_str = f"Modo {latest.mode_used}"
            item_count_str = str(len(latest.digest.items))
            posted_str = "Sim" if latest.posted_at is not None else "Nao"
        elif scheduler and scheduler.last_run_at:
            gen_local = scheduler.last_run_at.astimezone(tz).strftime("%d/%m/%Y %H:%M")
            mode_used_str = f"Modo {scheduler.last_mode_used}" if scheduler.last_mode_used else "N/A"
            item_count_str = str(scheduler.last_run_count or 0)
            posted_str = "Sim" if scheduler.last_post_ok else "Nao"
        else:
            gen_local = "Nenhuma geracao registrada ainda"
            mode_used_str = "N/A"
            item_count_str = "0"
            posted_str = "Nao"

        ai_cfg_str = (
            f"{self.bot.config.ai_mode} ({self.bot.config.gemini_model})"
            if self.bot.config.ai_mode != "off"
            else "off"
        )

        lines = [
            f"**Proximo envio:** {next_run_str}",
            f"**Fontes RSS/HN ativas:** {self.bot.feeds_config.total_sources}",
            f"**Modo IA configurado:** {ai_cfg_str}",
            f"**Ultimo modo usado:** {mode_used_str}",
            f"**Ultima geracao:** {gen_local}",
            f"**Itens no ultimo digest:** {item_count_str}",
            f"**Ultimo envio concluido:** {posted_str}",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)
