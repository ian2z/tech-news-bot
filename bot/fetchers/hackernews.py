from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from bot.fetchers import fetch_with_retry
from bot.models import Article, clean_html, make_article_id, normalize_url

logger = logging.getLogger(__name__)

HN_TOP_STORIES_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{item_id}.json"


async def _fetch_item(
    client: httpx.AsyncClient,
    item_id: int,
    semaphore: asyncio.Semaphore,
    *,
    retries: int = 2,
    base_delay: float = 0.5,
) -> dict[str, Any] | None:
    async with semaphore:
        try:
            response = await fetch_with_retry(
                client,
                HN_ITEM_URL.format(item_id=item_id),
                retries=retries,
                base_delay=base_delay,
            )
            data = response.json()
            return data if isinstance(data, dict) else None
        except Exception as exc:
            logger.debug("Falha ao buscar item HN %s: %s", item_id, exc)
            return None


async def fetch_hackernews(
    client: httpx.AsyncClient,
    *,
    top_n: int = 10,
    min_score: int = 150,
    category: str = "dev",
    retries: int = 3,
    base_delay: float = 1.0,
) -> list[Article]:
    response = await fetch_with_retry(
        client,
        HN_TOP_STORIES_URL,
        retries=retries,
        base_delay=base_delay,
    )
    story_ids = response.json()
    if not isinstance(story_ids, list):
        raise ValueError("Resposta inesperada da API do Hacker News")

    candidate_limit = max(top_n * 3, 30)
    candidate_ids = [int(sid) for sid in story_ids[:candidate_limit]]

    semaphore = asyncio.Semaphore(10)
    items = await asyncio.gather(
        *(_fetch_item(client, sid, semaphore) for sid in candidate_ids)
    )

    articles: list[Article] = []
    for item in items:
        if not item:
            continue
        if item.get("type") != "story":
            continue
        if item.get("dead") or item.get("deleted"):
            continue

        score = int(item.get("score") or 0)
        if score < min_score:
            continue

        raw_title = (item.get("title") or "").strip()
        raw_url = (item.get("url") or "").strip()
        if not raw_title or not raw_url:
            continue

        title = clean_html(raw_title)
        if not title:
            continue

        timestamp = item.get("time")
        if timestamp is not None:
            published = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
        else:
            published = datetime.now(timezone.utc)

        normalized_url = normalize_url(raw_url)
        article_id = make_article_id(normalized_url)
        descendants = int(item.get("descendants") or 0)
        hn_summary = f"{score} pontos e {descendants} comentarios no Hacker News."

        articles.append(
            Article(
                id=article_id,
                title=title,
                url=normalized_url,
                source="Hacker News",
                published=published,
                summary=hn_summary,
                image_url=None,
                score=score,
                lang="en",
                category=category,
            )
        )
        if len(articles) >= top_n:
            break

    return articles
