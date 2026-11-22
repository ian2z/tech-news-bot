from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from bot.ai_schema import ValidatedDigest, ValidatedNewsItem
from bot.formatter import (
    MAX_DESCRIPTION_CHARS,
    MAX_EMBED_TOTAL_CHARS,
    MAX_EMBEDS_PER_MESSAGE,
    build_category_embeds,
    embed_char_count,
    format_digest_messages,
)


def _make_item(
    idx: int,
    *,
    title: str | None = None,
    summary: str = "Resumo objetivo da noticia.",
    category: str = "ai",
    lang: str = "EN",
    confidence: str = "alta",
    origin: str = "ai",
) -> ValidatedNewsItem:
    return ValidatedNewsItem(
        rank=idx,
        title=title or f"Titulo da noticia {idx}",
        summary=summary,
        why_it_matters="",
        category=category,  # type: ignore[arg-type]
        lang=lang,  # type: ignore[arg-type]
        source_name="TechCrunch",
        url=f"https://example.com/news/{idx}",
        published_at="2026-11-22T10:00:00+00:00",
        confidence=confidence,  # type: ignore[arg-type]
        corroborating_sources=[],
        image_url=None,
        origin=origin,  # type: ignore[arg-type]
    )


def test_build_category_embeds_groups_items_with_flags_and_colors() -> None:
    now = datetime(2026, 11, 22, 8, 0, tzinfo=ZoneInfo("America/Fortaleza"))
    items = [
        _make_item(1, category="sec", lang="EN", confidence="alta", origin="ai"),
        _make_item(2, category="sec", lang="PT-BR", origin="feed"),
    ]
    embeds = build_category_embeds("sec", items, updated_at=now)
    assert len(embeds) == 1
    embed = embeds[0]
    assert embed.color is not None and embed.color.value == 0xEF4444
    assert embed.author.name == "🛡️ CIBERSEGURANÇA & VULNERABILIDADES [SEC]"
    assert embed.description is not None
    assert "🇺🇸" in embed.description
    assert "🇧🇷" in embed.description
    assert "✅ Verificada" in embed.description
    assert embed.footer.text == "Resumo Tech 24h • 2 destaques • Atualizado às 08:00"


def test_format_digest_orders_critical_first_and_groups_by_category() -> None:
    digest = ValidatedDigest(
        date="2026-11-22",
        intro="Dia movimentado em seguranca e IA.",
        items=[
            _make_item(1, category="tech"),
            _make_item(2, category="ai"),
            _make_item(3, category="critico"),
            _make_item(4, category="ai"),
        ],
    )
    tz = ZoneInfo("America/Fortaleza")
    messages = format_digest_messages(
        digest,
        tz=tz,
        now=datetime(2026, 11, 22, 11, 0, tzinfo=timezone.utc),
    )

    assert len(messages) == 1
    content, embeds = messages[0]
    assert content is not None
    assert content.startswith("☀️ **Bom dia! Aqui está o seu Radar Tech de 22/11/2026:**")
    assert len(embeds) == 3
    # Critical must appear first, then ai, then tech
    assert embeds[0].author.name and "CRÍTICO" in embeds[0].author.name
    assert embeds[1].author.name and "AI / LLM" in embeds[1].author.name
    assert embeds[2].author.name and "TECH" in embeds[2].author.name


def test_format_digest_splits_when_exceeding_6000_total_chars() -> None:
    items = []
    for idx, cat in enumerate(("critico", "sec", "ai", "dev", "tech"), start=1):
        for sub in range(6):
            items.append(_make_item(idx * 10 + sub, category=cat, summary="X" * 200))

    digest = ValidatedDigest(date="2026-11-22", intro="Intro", items=items)
    tz = ZoneInfo("America/Fortaleza")
    messages = format_digest_messages(digest, tz=tz)

    assert len(messages) >= 2
    for _, embeds in messages:
        total = sum(embed_char_count(e) for e in embeds)
        assert total <= MAX_EMBED_TOTAL_CHARS
        assert len(embeds) <= MAX_EMBEDS_PER_MESSAGE
        for e in embeds:
            assert e.description is None or len(e.description) <= MAX_DESCRIPTION_CHARS
