from __future__ import annotations

import calendar
import logging
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx

from bot.fetchers import fetch_with_retry
from bot.models import Article, clean_html, make_article_id, normalize_url

logger = logging.getLogger(__name__)


def _extract_published(entry: Any) -> datetime:
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed is None and isinstance(entry, dict):
            parsed = entry.get(attr)
        if parsed is not None:
            try:
                timestamp = calendar.timegm(parsed)
                return datetime.fromtimestamp(timestamp, tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                continue
    return datetime.now(timezone.utc)


def _extract_image_url(entry: Any) -> str | None:
    media_thumbnail = entry.get("media_thumbnail") if isinstance(entry, dict) else getattr(entry, "media_thumbnail", None)
    if media_thumbnail and isinstance(media_thumbnail, list):
        for item in media_thumbnail:
            url = item.get("url") if isinstance(item, dict) else None
            if url and isinstance(url, str) and url.startswith(("http://", "https://")):
                return url

    media_content = entry.get("media_content") if isinstance(entry, dict) else getattr(entry, "media_content", None)
    if media_content and isinstance(media_content, list):
        for item in media_content:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            medium = item.get("medium", "")
            mime = item.get("type", "")
            if url and isinstance(url, str) and url.startswith(("http://", "https://")):
                if medium == "image" or mime.startswith("image/") or not (medium or mime):
                    return url

    enclosures = entry.get("enclosures") if isinstance(entry, dict) else getattr(entry, "enclosures", None)
    if enclosures and isinstance(enclosures, list):
        for enc in enclosures:
            if not isinstance(enc, dict):
                continue
            href = enc.get("href") or enc.get("url")
            mime = enc.get("type", "")
            if href and isinstance(href, str) and href.startswith(("http://", "https://")) and mime.startswith("image/"):
                return href

    return None


def parse_rss_content(
    content: str | bytes,
    source_name: str,
    lang: str = "en",
    category: str = "tech",
) -> list[Article]:
    parsed = feedparser.parse(content)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"Feed RSS invalido ou vazio para {source_name}: {parsed.bozo_exception}")

    articles: list[Article] = []
    for entry in parsed.entries:
        raw_title = (entry.get("title") or "").strip()
        raw_link = (entry.get("link") or "").strip()
        if not raw_title or not raw_link:
            continue

        title = clean_html(raw_title)
        if not title:
            continue

        normalized_url = normalize_url(raw_link)
        article_id = make_article_id(normalized_url)
        published = _extract_published(entry)

        raw_summary = entry.get("summary") or entry.get("description")
        if not raw_summary and entry.get("content"):
            content_list = entry.get("content")
            if isinstance(content_list, list) and content_list:
                raw_summary = content_list[0].get("value")

        summary = clean_html(raw_summary)
        image_url = _extract_image_url(entry)

        articles.append(
            Article(
                id=article_id,
                title=title,
                url=normalized_url,
                source=source_name,
                published=published,
                summary=summary,
                image_url=image_url,
                score=0,
                lang=lang,
                category=category,
            )
        )
    return articles


async def fetch_rss_feed(
    client: httpx.AsyncClient,
    source_name: str,
    url: str,
    lang: str = "en",
    category: str = "tech",
    *,
    retries: int = 3,
    base_delay: float = 1.0,
) -> list[Article]:
    response = await fetch_with_retry(client, url, retries=retries, base_delay=base_delay)
    return parse_rss_content(response.content, source_name=source_name, lang=lang, category=category)
