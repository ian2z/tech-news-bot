from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, time, timezone
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, tasks

from bot.ai_schema import ValidatedDigest
from bot.dedup import CachedDigest
from bot.formatter import format_digest_messages
from bot.pipeline import AllSourcesFailedError, run_digest_pipeline

if TYPE_CHECKING:
    from bot.main import TechNewsBot

logger = logging.getLogger(__name__)

RETRY_DELAY_SECONDS = 15 * 60
MAX_RETRY_ATTEMPTS = 3


class NewsSchedulerCog(commands.Cog):
    def __init__(self, bot: TechNewsBot) -> None:
        self.bot = bot
        self.last_run_at: datetime | None = None
        self.last_run_count: int | None = None
        self.last_mode_used: str | None = None
        self.last_post_ok: bool | None = None
        self.last_run_status: str = "Nenhuma execucao ainda"
        self._run_lock = asyncio.Lock()
        self._retry_task: asyncio.Task[None] | None = None

        self.daily_news.change_interval(time=self.bot.config.post_time)

    async def cog_load(self) -> None:
        latest = await self.bot.dedup_store.get_latest_digest()
        if latest is not None:
            self.last_run_at = latest.created_at
            self.last_run_count = len(latest.digest.items)
            self.last_mode_used = latest.mode_used
            self.last_post_ok = latest.posted_at is not None
            self.last_run_status = (
                f"{len(latest.digest.items)} noticias (Modo {latest.mode_used})"
                if latest.posted_at is not None
                else f"Cache gerado (Modo {latest.mode_used}), ainda nao postado"
            )
        self.daily_news.start()

    async def cog_unload(self) -> None:
        self.daily_news.cancel()
        if self._retry_task and not self._retry_task.done():
            self._retry_task.cancel()

    async def _resolve_channel(self) -> discord.abc.Messageable | None:
        channel_id = self.bot.config.discord_channel_id
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.NotFound:
                logger.error("Canal configurado (%s) nao encontrado.", channel_id)
                return None
            except discord.Forbidden:
                logger.error("Sem permissao para acessar o canal configurado (%s).", channel_id)
                return None
            except discord.HTTPException as exc:
                logger.error("Erro HTTP ao buscar canal (%s): %s", channel_id, exc)
                return None

        if not isinstance(channel, discord.abc.Messageable):
            logger.error("Canal configurado (%s) nao aceita envio de mensagens.", channel_id)
            return None
        return channel

    async def _post_digest(
        self,
        channel: discord.abc.Messageable,
        date_str: str,
        digest: ValidatedDigest,
        mode_used: str,
        *,
        origin: str,
    ) -> tuple[int, str]:
        messages = format_digest_messages(digest, tz=self.bot.config.timezone)
        try:
            for content, embeds in messages:
                await channel.send(
                    content=content,
                    embeds=embeds,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except discord.Forbidden:
            msg = "Sem permissao para enviar mensagens ou embeds no canal configurado."
            logger.error(msg)
            self.last_run_count = 0
            self.last_post_ok = False
            self.last_run_status = msg
            return 0, msg
        except discord.HTTPException as exc:
            msg = f"Erro ao enviar mensagem para o Discord: {exc}"
            logger.error(msg)
            self.last_run_count = 0
            self.last_post_ok = False
            self.last_run_status = msg
            return 0, msg

        await self.bot.dedup_store.mark_digest_sent(date_str, digest)
        count = len(digest.items)
        msg = f"{count} noticias postadas (Modo {mode_used}, {origin})."
        logger.info("Execucao concluida: %s", msg)
        self.last_run_count = count
        self.last_mode_used = mode_used
        self.last_post_ok = True
        self.last_run_status = msg
        return count, msg

    async def run_and_post(
        self,
        *,
        manual: bool = False,
        force: bool = False,
    ) -> tuple[int, str]:
        async with self._run_lock:
            now_utc = datetime.now(timezone.utc)
            date_str = now_utc.astimezone(self.bot.config.timezone).strftime("%Y-%m-%d")
            cached: CachedDigest | None = await self.bot.dedup_store.get_digest(date_str)

            # Manual /noticias cache and cooldown rules
            if manual and cached is not None:
                if cached.posted_at is not None and not force:
                    return (
                        0,
                        "O briefing de hoje ja foi publicado. Use `/noticias forcar:True` para regenerar.",
                    )

                if cached.posted_at is None and not force:
                    channel = await self._resolve_channel()
                    if channel is None:
                        msg = "Canal configurado indisponivel ou sem permissao de acesso."
                        self.last_post_ok = False
                        self.last_run_status = msg
                        return 0, msg
                    self.last_run_at = cached.created_at
                    return await self._post_digest(
                        channel,
                        date_str,
                        cached.digest,
                        cached.mode_used,
                        origin="cache",
                    )

                if force and self.bot.config.manual_cooldown_minutes > 0:
                    elapsed_seconds = (now_utc - cached.created_at).total_seconds()
                    cooldown_seconds = self.bot.config.manual_cooldown_minutes * 60
                    if elapsed_seconds < cooldown_seconds:
                        remaining_min = max(1, math.ceil((cooldown_seconds - elapsed_seconds) / 60))
                        return (
                            0,
                            f"Cooldown ativo: aguarde mais {remaining_min} minuto(s) para regenerar o briefing.",
                        )

            self.last_run_at = now_utc

            channel = await self._resolve_channel()
            if channel is None:
                msg = "Canal configurado indisponivel ou sem permissao de acesso."
                self.last_run_count = 0
                self.last_post_ok = False
                self.last_run_status = msg
                return 0, msg

            digest, mode_used = await run_digest_pipeline(
                self.bot.config,
                self.bot.feeds_config,
                self.bot.dedup_store,
                curator=self.bot.curator,
                now=now_utc,
            )

            self.last_mode_used = mode_used

            if digest is None or not digest.items:
                msg = "Nenhuma noticia nova encontrada."
                logger.info("Coleta concluida (Modo %s): %s", mode_used, msg)
                self.last_run_count = 0
                self.last_post_ok = False
                self.last_run_status = msg
                return 0, msg

            await self.bot.dedup_store.save_digest(
                date_str,
                mode_used,
                digest,
                created_at=now_utc,
            )

            origin = "manual/forcar" if (manual and force) else ("manual" if manual else "agendada")
            return await self._post_digest(channel, date_str, digest, mode_used, origin=origin)

    @tasks.loop(time=time(hour=8, minute=0))
    async def daily_news(self) -> None:
        try:
            await self.run_and_post(manual=False, force=False)
        except AllSourcesFailedError as exc:
            self.last_run_count = 0
            self.last_post_ok = False
            self.last_run_status = f"Todas as fontes falharam (tentativa 1/{MAX_RETRY_ATTEMPTS})"
            logger.error("%s (tentativa 1/%d). Reagendando em 15 minutos.", exc, MAX_RETRY_ATTEMPTS)
            if self._retry_task and not self._retry_task.done():
                self._retry_task.cancel()
            self._retry_task = asyncio.create_task(self._retry_after_failure())
        except Exception as exc:
            self.last_run_count = 0
            self.last_post_ok = False
            self.last_run_status = f"Erro inesperado: {exc}"
            logger.exception("Erro inesperado na task diaria de noticias.")

    async def _retry_after_failure(self) -> None:
        for attempt in range(2, MAX_RETRY_ATTEMPTS + 1):
            await asyncio.sleep(RETRY_DELAY_SECONDS)
            try:
                await self.run_and_post(manual=False, force=False)
                return
            except AllSourcesFailedError as exc:
                self.last_run_count = 0
                self.last_post_ok = False
                self.last_run_status = f"Todas as fontes falharam (tentativa {attempt}/{MAX_RETRY_ATTEMPTS})"
                logger.error("%s (tentativa %d/%d).", exc, attempt, MAX_RETRY_ATTEMPTS)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_run_count = 0
                self.last_post_ok = False
                self.last_run_status = f"Erro inesperado: {exc}"
                logger.exception("Erro inesperado durante retentativa da coleta.")
                return

    @daily_news.before_loop
    async def before_daily_news(self) -> None:
        await self.bot.wait_until_ready()
        logger.info(
            "Proximo envio agendado para %s (%s)",
            self.bot.config.post_time_str,
            self.bot.config.timezone_name,
        )

    @daily_news.error
    async def daily_news_error(self, error: BaseException) -> None:
        logger.exception("Erro nao tratado no loop daily_news: %s", error)
