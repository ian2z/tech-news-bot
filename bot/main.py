from __future__ import annotations

import logging
import sys

import discord
from discord.ext import commands

from bot.commands import NewsCommandsCog
from bot.config import Config, ConfigError, load_config, setup_logging
from bot.curator import GeminiCurator
from bot.dedup import DedupStore
from bot.pipeline import FeedsConfig, load_feeds_config
from bot.scheduler import NewsSchedulerCog

logger = logging.getLogger(__name__)


class TechNewsBot(commands.Bot):
    def __init__(self, config: Config, feeds_config: FeedsConfig) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.config = config
        self.feeds_config = feeds_config
        self.dedup_store = DedupStore(config.db_path)
        self.curator = (
            GeminiCurator(
                api_key=config.gemini_api_key,
                model=config.gemini_model,
                timeout_seconds=config.ai_timeout_seconds,
                max_retries=config.ai_max_retries,
            )
            if config.ai_mode != "off" and config.gemini_api_key
            else None
        )

    async def setup_hook(self) -> None:
        await self.dedup_store.init()
        await self.add_cog(NewsSchedulerCog(self))
        await self.add_cog(NewsCommandsCog(self))

        guild = discord.Object(id=self.config.discord_guild_id)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        logger.info(
            "Comandos slash sincronizados no servidor %s: %s",
            self.config.discord_guild_id,
            ", ".join(f"/{cmd.name}" for cmd in synced) or "nenhum",
        )

    async def on_ready(self) -> None:
        logger.info("Bot conectado como %s (id=%s)", self.user, self.user.id if self.user else "?")


def main() -> None:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Erro de configuracao: {exc}", file=sys.stderr)
        sys.exit(1)

    setup_logging(config.log_level)

    try:
        feeds_config = load_feeds_config(config.feeds_path)
    except Exception as exc:
        logger.error("Erro ao carregar %s: %s", config.feeds_path, exc)
        sys.exit(1)

    logger.info(
        "Iniciando Tech News Bot com %d fontes configuradas (AI_MODE=%s, modelo=%s).",
        feeds_config.total_sources,
        config.ai_mode,
        config.gemini_model if config.ai_mode != "off" else "nenhum",
    )

    bot = TechNewsBot(config=config, feeds_config=feeds_config)
    bot.run(config.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
