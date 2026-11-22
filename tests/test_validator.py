from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from bot.ai_schema import Digest, NewsItem, Source
from bot.models import make_article_id
from bot.validator import validate_digest


def _item(
    rank: int,
    url: str,
    *,
    title: str = "Noticia de tecnologia valida",
    summary: str = "Resumo claro.",
    why: str = "Importante para o mercado.",
    category: str = "ai",
    confidence: str = "alta",
    published_at: str | None = "2026-11-22T10:00:00+00:00",
    corrob: list[Source] | None = None,
) -> NewsItem:
    return NewsItem(
        rank=rank,
        title=title,
        summary=summary,
        why_it_matters=why,
        category=category,  # type: ignore[arg-type]
        lang="EN",
        source_name="TechCrunch",
        url=url,
        published_at=published_at,
        confidence=confidence,  # type: ignore[arg-type]
        corroborating_sources=corrob or [],
    )


@pytest.mark.asyncio
async def test_validator_url_rules_redirects_og_image_and_sanitization() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://news.example/redirect":
            return httpx.Response(302, headers={"Location": "https://canonical.example/story?utm_source=x"})
        if url == "https://canonical.example/story?utm_source=x":
            html = '<html><head><meta property="og:image" content="https://cdn.example/img.jpg"></head></html>'
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})
        if url == "https://news.example/notfound":
            return httpx.Response(404, text="Not Found")
        if url == "https://news.example/blocked-high":
            return httpx.Response(403, text="Forbidden")
        if url == "https://news.example/blocked-low":
            return httpx.Response(403, text="Forbidden")
        return httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"})

    digest = Digest(
        date="2026-11-22",
        intro="<b>Resumo</b> @everyone do dia.",
        items=[
            _item(
                1,
                "https://news.example/redirect",
                title="<b>Titulo</b> @here " + ("A" * 150),
                summary="<p>Resumo @everyone " + ("B" * 350) + "</p>",
                why="<i>Impacto</i> " + ("C" * 180),
            ),
            _item(2, "https://news.example/notfound", title="Pagina 404"),
            _item(3, "https://news.example/blocked-high", title="Bloqueado mas confianca media", confidence="média"),
            _item(4, "https://news.example/blocked-low", title="Bloqueado e confianca baixa", confidence="baixa"),
            _item(5, "https://192.168.1.10/secret", title="Host IP deve falhar"),
            _item(6, "https://bit.ly/3abc123", title="Encurtador deve falhar"),
        ],
    )

    transport = httpx.MockTransport(handler)
    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        validated = await validate_digest(
            digest,
            mode_used="A",
            sent_ids=set(),
            already_sent_titles=[],
            max_age_hours=48,
            client=client,
            now=now,
        )

    assert len(validated.items) == 2
    first, second = validated.items
    assert first.url == "https://canonical.example/story"
    assert first.image_url == "https://cdn.example/img.jpg"
    assert len(first.title) <= 110
    assert len(first.summary) <= 300
    assert len(first.why_it_matters) <= 140
    assert "@everyone" not in first.summary
    assert "<b>" not in first.title
    assert "@everyone" not in validated.intro

    assert second.url == "https://news.example/blocked-high"
    assert second.rank == 2


@pytest.mark.asyncio
async def test_validator_mode_b_rejects_url_not_in_candidates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"})

    digest = Digest(
        date="2026-11-22",
        intro="Intro",
        items=[
            _item(1, "https://allowed.example/post-1", title="Item valido do candidato"),
            _item(2, "https://invented.example/post-2", title="Item inventado fora dos candidatos"),
        ],
    )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        validated = await validate_digest(
            digest,
            mode_used="B",
            sent_ids=set(),
            already_sent_titles=[],
            candidate_urls={"https://allowed.example/post-1"},
            client=client,
            now=datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc),
        )

    assert [it.url for it in validated.items] == ["https://allowed.example/post-1"]


@pytest.mark.asyncio
async def test_validator_blocks_corroborating_dup_title_dup_and_old_items() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"})

    already_sent_corrob_url = "https://other.example/already-sent"
    sent_ids = {make_article_id(already_sent_corrob_url)}

    digest = Digest(
        date="2026-11-22",
        intro="Intro",
        items=[
            _item(
                1,
                "https://news.example/new-url",
                title="Historia repetida via corroborating",
                corrob=[Source(name="Other", url=already_sent_corrob_url)],
            ),
            _item(
                2,
                "https://news.example/similar",
                title="OpenAI lanca novo modelo compacto para desenvolvedores",
            ),
            _item(
                3,
                "https://news.example/old",
                title="Noticia muito antiga",
                published_at="2026-11-15T10:00:00+00:00",
            ),
            _item(
                4,
                "https://news.example/fresh",
                title="Linux 7.2 traz melhorias para chips Rockchip",
            ),
        ],
    )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        validated = await validate_digest(
            digest,
            mode_used="A",
            sent_ids=sent_ids,
            already_sent_titles=["OpenAI lanca novo modelo compacto para desenvolvedor"],
            max_age_hours=48,
            client=client,
            now=datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc),
        )

    assert [it.url for it in validated.items] == ["https://news.example/fresh"]


def test_promote_critical_category_for_zero_day_and_rce() -> None:
    from bot.validator import promote_critical_category

    assert promote_critical_category("sec", "Zero-Day RCE encontrado no OpenSSH") == "critico"
    assert promote_critical_category("sec", "Nova diretriz de senhas da NIST") == "sec"
    assert promote_critical_category("ai", "Zero-Day discussion in LLM paper") == "ai"
