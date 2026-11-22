from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Category = Literal["critico", "sec", "ai", "dev", "tech"]
Lang = Literal["PT-BR", "EN"]
Confidence = Literal["alta", "média", "baixa"]

CATEGORY_ORDER: tuple[Category, ...] = ("critico", "sec", "ai", "dev", "tech")

TITLE_MAX_LEN = 110
SUMMARY_MAX_LEN = 220
WHY_IT_MATTERS_MAX_LEN = 140
INTRO_MAX_LEN = 240


class Source(BaseModel):
    name: str
    url: str


class NewsItem(BaseModel):
    rank: int
    title: str
    summary: str
    why_it_matters: str = ""
    category: Category
    lang: Lang = "EN"
    source_name: str
    url: str
    published_at: Optional[str] = None
    confidence: Confidence = "média"
    corroborating_sources: list[Source] = Field(default_factory=list)


class Digest(BaseModel):
    date: str
    intro: str
    items: list[NewsItem]


class ValidatedNewsItem(NewsItem):
    """Internal enriched news item after URL validation and og:image extraction."""

    image_url: Optional[str] = None
    origin: Literal["ai", "feed"] = "ai"


class ValidatedDigest(BaseModel):
    """Validated digest ready for caching and Discord formatting."""

    date: str
    intro: str
    items: list[ValidatedNewsItem]
