from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot.models import Article, make_article_id
from bot.pipeline import deduplicate_batch, filter_articles, rank_and_diversify


def _make_article(
    title: str,
    url: str,
    source: str,
    hours_ago: float = 1.0,
    score: int = 0,
    now: datetime | None = None,
) -> Article:
    ref = now or datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
    return Article(
        id=make_article_id(url),
        title=title,
        url=url,
        source=source,
        published=ref - timedelta(hours=hours_ago),
        summary="Resumo de teste.",
        score=score,
    )


def test_filter_articles_removes_old_and_invalid() -> None:
    now = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
    fresh = _make_article("Noticia recente", "https://a.com/1", "A", hours_ago=5, now=now)
    old = _make_article("Noticia velha", "https://a.com/2", "A", hours_ago=35, now=now)
    empty_title = _make_article("   ", "https://a.com/3", "A", hours_ago=1, now=now)

    result = filter_articles([fresh, old, empty_title], max_age_hours=30, now=now)
    assert [a.url for a in result] == ["https://a.com/1"]


def test_deduplicate_batch_merges_similar_titles_and_counts_sources() -> None:
    a1 = _make_article(
        "OpenAI launches new lightweight reasoning model for developers",
        "https://techcrunch.com/openai-model",
        "TechCrunch",
    )
    a2 = _make_article(
        "OpenAI launches new lightweight reasoning model for developer",
        "https://theverge.com/openai-model",
        "The Verge",
    )
    a3 = _make_article(
        "Linux kernel 7.2 adds improved Rockchip ARM64 support",
        "https://arstechnica.com/linux-rockchip",
        "Ars Technica",
    )

    deduped = deduplicate_batch([a1, a2, a3])
    assert len(deduped) == 2

    counts_by_title = {article.title: count for article, count in deduped}
    assert counts_by_title[a1.title] == 2
    assert counts_by_title[a3.title] == 1


def test_rank_and_diversify_caps_single_source() -> None:
    now = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
    items = [
        (_make_article(f"TechCrunch {i}", f"https://tc.com/{i}", "TechCrunch", hours_ago=0.1 * i, now=now), 1)
        for i in range(1, 7)
    ]
    items.extend(
        [
            (_make_article("Wired 1", "https://wired.com/1", "Wired", hours_ago=5, now=now), 1),
            (_make_article("Verge 1", "https://verge.com/1", "The Verge", hours_ago=6, now=now), 1),
            (_make_article("HN 1", "https://hn.example/1", "Hacker News", hours_ago=4, score=450, now=now), 1),
            (_make_article("Tecnoblog 1", "https://tb.com/1", "Tecnoblog", hours_ago=7, now=now), 1),
        ]
    )

    selected = rank_and_diversify(items, max_articles=6, max_age_hours=30, now=now)
    assert len(selected) == 6
    tc_count = sum(1 for a in selected if a.source == "TechCrunch")
    assert tc_count <= 2
