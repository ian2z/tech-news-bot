from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import discord
import pytest

from bot.ai_schema import ValidatedDigest, ValidatedNewsItem
from bot.config import Config
from bot.dedup import DedupStore
from bot.pipeline import FeedsConfig, HackerNewsConfig
from bot.scheduler import NewsSchedulerCog


class _FakeChannel(discord.abc.Messageable):
    def __init__(self) -> None:
        self.sent_messages: list[dict[str, Any]] = []

    async def _get_channel(self) -> Any:
        return self

    async def send(self, content: Any = None, **kwargs: Any) -> Any:
        self.sent_messages.append({"content": content, **kwargs})
        return SimpleNamespace(id=1)


def _make_config(db_path: Path, cooldown: int = 60) -> Config:
    return Config(
        discord_token="tok",
        discord_channel_id=123,
        discord_guild_id=456,
        post_hour=8,
        post_minute=30,
        timezone_name="America/Fortaleza",
        timezone=ZoneInfo("America/Fortaleza"),
        db_path=str(db_path),
        ai_mode="off",
        manual_cooldown_minutes=cooldown,
    )


def _sample_validated_digest(date_str: str) -> ValidatedDigest:
    return ValidatedDigest(
        date=date_str,
        intro="Resumo do dia.",
        items=[
            ValidatedNewsItem(
                rank=1,
                title="Noticia cacheada",
                summary="Resumo.",
                why_it_matters="Importa.",
                category="ai",
                lang="EN",
                source_name="TechCrunch",
                url="https://techcrunch.com/cached",
                published_at="2026-11-22T10:00:00+00:00",
                confidence="alta",
                origin="ai",
            )
        ],
    )


@pytest.mark.asyncio
async def test_manual_noticias_reuses_cache_blocks_repost_and_enforces_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "sent.db"
    store = DedupStore(str(db_path))
    await store.init()

    cfg = _make_config(db_path, cooldown=60)
    channel = _FakeChannel()
    bot = SimpleNamespace(
        config=cfg,
        feeds_config=FeedsConfig(rss=[], hackernews=HackerNewsConfig(enabled=False)),
        dedup_store=store,
        curator=None,
        get_channel=lambda cid: channel,
    )

    pipeline_calls = 0

    async def _fake_pipeline(*args: Any, **kwargs: Any) -> tuple[ValidatedDigest, str]:
        nonlocal pipeline_calls
        pipeline_calls += 1
        today = datetime.now(cfg.timezone).strftime("%Y-%m-%d")
        return _sample_validated_digest(today), "A"

    monkeypatch.setattr("bot.scheduler.run_digest_pipeline", _fake_pipeline)

    cog = NewsSchedulerCog(bot)  # type: ignore[arg-type]
    today_str = datetime.now(cfg.timezone).strftime("%Y-%m-%d")

    # 1. Pre-populate unposted cache -> manual /noticias must post cache without calling pipeline
    await store.save_digest(today_str, "A", _sample_validated_digest(today_str))
    count, msg = await cog.run_and_post(manual=True, force=False)
    assert count == 1
    assert pipeline_calls == 0
    assert len(channel.sent_messages) == 1

    # 2. Call /noticias again without force -> must NOT repost
    count2, msg2 = await cog.run_and_post(manual=True, force=False)
    assert count2 == 0
    assert "forcar:True" in msg2
    assert len(channel.sent_messages) == 1

    # 3. Call /noticias with force=True inside cooldown -> must be rejected
    count3, msg3 = await cog.run_and_post(manual=True, force=True)
    assert count3 == 0
    assert "Cooldown ativo" in msg3
    assert pipeline_calls == 0

    # 4. Backdate created_at beyond cooldown -> force=True must regenerate and post
    old_created = datetime.now(timezone.utc) - timedelta(minutes=120)
    await store.save_digest(today_str, "A", _sample_validated_digest(today_str), created_at=old_created)
    await store.mark_digest_sent(today_str, _sample_validated_digest(today_str))
    count4, _ = await cog.run_and_post(manual=True, force=True)
    assert count4 == 1
    assert pipeline_calls == 1
    assert len(channel.sent_messages) == 2
