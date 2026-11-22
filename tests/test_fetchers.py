from __future__ import annotations

import httpx
import pytest

from bot.fetchers.hackernews import fetch_hackernews
from bot.fetchers.rss import fetch_rss_feed
from bot.pipeline import AllSourcesFailedError, FeedsConfig, HackerNewsConfig, RSSFeedConfig, collect_from_sources

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
  <channel>
    <title>Mock Tech Feed</title>
    <item>
      <title>Armbian launches optimized build for Rockchip</title>
      <link>https://example.com/armbian-rockchip/?utm_source=rss</link>
      <description>&lt;p&gt;Great performance improvements.&lt;/p&gt;</description>
      <pubDate>Sat, 21 Nov 2026 10:00:00 GMT</pubDate>
      <media:thumbnail url="https://example.com/thumb.jpg" />
    </item>
  </channel>
</rss>
"""


@pytest.mark.asyncio
async def test_fetch_rss_feed_valid() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=SAMPLE_RSS)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        articles = await fetch_rss_feed(client, "MockFeed", "https://example.com/feed", retries=1, base_delay=0)

    assert len(articles) == 1
    article = articles[0]
    assert article.title == "Armbian launches optimized build for Rockchip"
    assert article.url == "https://example.com/armbian-rockchip"
    assert article.summary == "Great performance improvements."
    assert article.image_url == "https://example.com/thumb.jpg"
    assert article.source == "MockFeed"


@pytest.mark.asyncio
async def test_fetch_rss_feed_broken_xml_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<<< not valid xml >>>")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ValueError, match="invalido"):
            await fetch_rss_feed(client, "BrokenFeed", "https://example.com/broken", retries=1, base_delay=0)


@pytest.mark.asyncio
async def test_fetch_rss_feed_timeout_retries_and_fails() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("timed out")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.ReadTimeout):
            await fetch_rss_feed(client, "SlowFeed", "https://example.com/slow", retries=3, base_delay=0)

    assert attempts == 3


@pytest.mark.asyncio
async def test_fetch_hackernews_filters_by_min_score() -> None:
    items = {
        1: {"id": 1, "type": "story", "title": "High Score Story", "url": "https://hn.test/1", "score": 320, "descendants": 80, "time": 1795168800},
        2: {"id": 2, "type": "story", "title": "Low Score Story", "url": "https://hn.test/2", "score": 50, "descendants": 5, "time": 1795168800},
        3: {"id": 3, "type": "story", "title": "Ask HN without URL", "score": 400, "time": 1795168800},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("topstories.json"):
            return httpx.Response(200, json=[1, 2, 3])
        item_id = int(request.url.path.split("/")[-1].replace(".json", ""))
        return httpx.Response(200, json=items[item_id])

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        articles = await fetch_hackernews(client, top_n=5, min_score=150, retries=1, base_delay=0)

    assert len(articles) == 1
    assert articles[0].title == "High Score Story"
    assert articles[0].score == 320
    assert articles[0].source == "Hacker News"


@pytest.mark.asyncio
async def test_collect_from_sources_raises_when_all_fail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    feeds_cfg = FeedsConfig(
        rss=[RSSFeedConfig(name="Fail1", url="https://fail1.example/rss")],
        hackernews=HackerNewsConfig(enabled=False),
    )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(AllSourcesFailedError):
            await collect_from_sources(feeds_cfg, client=client, per_source_timeout=5.0)
