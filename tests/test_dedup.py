from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import pytest

from bot.ai_schema import Source, ValidatedDigest, ValidatedNewsItem
from bot.dedup import DedupStore
from bot.models import Article, make_article_id


@pytest.mark.asyncio
async def test_dedup_store_mark_sent_and_cleanup(tmp_path: Path) -> None:
    db_file = tmp_path / "sent.db"
    store = DedupStore(str(db_file), retention_days=60, digest_retention_days=30)
    await store.init()

    article = Article(
        id=make_article_id("https://example.com/post-1"),
        title="Post 1",
        url="https://example.com/post-1",
        source="TechCrunch",
        published=datetime.now(timezone.utc),
    )

    assert await store.get_sent_ids([article.id]) == set()
    await store.mark_sent([article])
    assert await store.get_sent_ids([article.id]) == {article.id}

    old_date = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    async with aiosqlite.connect(db_file) as db:
        await db.execute(
            "INSERT INTO sent_articles (id, url, title, source, sent_at) VALUES (?, ?, ?, ?, ?)",
            ("old-id", "https://example.com/old", "Old", "OldSource", old_date),
        )
        await db.commit()

    deleted = await store.init()
    assert deleted == 1
    assert await store.get_sent_ids(["old-id", article.id]) == {article.id}


@pytest.mark.asyncio
async def test_dedup_store_daily_digest_and_corroborating_sources(tmp_path: Path) -> None:
    db_file = tmp_path / "sent.db"
    store = DedupStore(str(db_file))
    await store.init()

    digest = ValidatedDigest(
        date="2026-11-22",
        intro="Intro",
        items=[
            ValidatedNewsItem(
                rank=1,
                title="Chip ARM64 anunciado",
                summary="Detalhes do chip.",
                why_it_matters="Menos consumo.",
                category="tech",
                lang="EN",
                source_name="TechCrunch",
                url="https://techcrunch.com/arm64",
                published_at="2026-11-22T10:00:00+00:00",
                confidence="alta",
                corroborating_sources=[
                    Source(name="The Verge", url="https://theverge.com/arm64-news")
                ],
                origin="ai",
            )
        ],
    )

    saved = await store.save_digest("2026-11-22", "A", digest)
    assert saved.posted_at is None
    assert saved.mode_used == "A"

    await store.mark_digest_sent("2026-11-22", digest)
    loaded = await store.get_digest("2026-11-22")
    assert loaded is not None
    assert loaded.posted_at is not None

    # Both primary and corroborating source URLs must be recorded in sent_articles
    sent_ids = await store.get_all_sent_ids()
    assert make_article_id("https://techcrunch.com/arm64") in sent_ids
    assert make_article_id("https://theverge.com/arm64-news") in sent_ids

    titles = await store.get_recent_titles(days=3)
    assert "Chip ARM64 anunciado" in titles
