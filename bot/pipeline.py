from __future__ import annotations

import asyncio
import difflib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx
import yaml

from bot.ai_schema import (
    SUMMARY_MAX_LEN,
    TITLE_MAX_LEN,
    Category,
    Digest,
    ValidatedDigest,
    ValidatedNewsItem,
)
from bot.config import Config
from bot.dedup import DedupStore
from bot.fetchers import DEFAULT_TIMEOUT, build_http_client
from bot.fetchers.hackernews import fetch_hackernews
from bot.fetchers.rss import fetch_rss_feed
from bot.models import Article, make_article_id, normalize_url
from bot.validator import promote_critical_category, sanitize_and_truncate, validate_digest

logger = logging.getLogger(__name__)

_TITLE_CLEAN_RE = re.compile(r"[^a-z0-9\s]+")
TITLE_SIMILARITY_THRESHOLD = 0.85
_VALID_FEED_CATEGORIES = {"critico", "sec", "ai", "dev", "tech"}


class AllSourcesFailedError(RuntimeError):
    """Raised when every configured news source fails during collection."""


class CuratorProtocol(Protocol):
    last_grounding_domains: set[str]

    async def curate(
        self,
        mode: str,
        n: int,
        already_sent: list[str],
        candidates: list[Article] | None,
        *,
        date_str: str = "",
        already_sent_days: int = 3,
        max_candidates: int = 50,
        timezone_name: str = "America/Fortaleza",
    ) -> tuple[Digest | None, str]: ...


@dataclass(frozen=True)
class RSSFeedConfig:
    name: str
    url: str
    lang: str = "en"
    category: str = "tech"


@dataclass(frozen=True)
class HackerNewsConfig:
    enabled: bool = True
    top_n: int = 10
    min_score: int = 150
    category: str = "dev"


@dataclass(frozen=True)
class FeedsConfig:
    rss: list[RSSFeedConfig] = field(default_factory=list)
    hackernews: HackerNewsConfig = field(default_factory=HackerNewsConfig)

    @property
    def total_sources(self) -> int:
        return len(self.rss) + (1 if self.hackernews.enabled else 0)


def _normalize_category(raw: Any, default: str = "tech") -> str:
    val = str(raw or default).strip().lower()
    return val if val in _VALID_FEED_CATEGORIES else default


def load_feeds_config(path: str | Path) -> FeedsConfig:
    data: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    rss_items: list[RSSFeedConfig] = []
    for entry in data.get("rss", []) or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        url = str(entry.get("url", "")).strip()
        lang = str(entry.get("lang", "en")).strip() or "en"
        category = _normalize_category(entry.get("category"), default="tech")
        if name and url:
            rss_items.append(RSSFeedConfig(name=name, url=url, lang=lang, category=category))

    hn_raw = data.get("hackernews") or {}
    hn_cfg = HackerNewsConfig(
        enabled=bool(hn_raw.get("enabled", True)),
        top_n=int(hn_raw.get("top_n", 10)),
        min_score=int(hn_raw.get("min_score", 150)),
        category=_normalize_category(hn_raw.get("category"), default="dev"),
    )
    return FeedsConfig(rss=rss_items, hackernews=hn_cfg)


def _normalize_title(title: str) -> str:
    lowered = title.lower()
    cleaned = _TITLE_CLEAN_RE.sub(" ", lowered)
    return " ".join(cleaned.split())


def filter_articles(
    articles: list[Article],
    *,
    max_age_hours: int,
    now: datetime | None = None,
) -> list[Article]:
    reference = now or datetime.now(timezone.utc)
    cutoff = reference - timedelta(hours=max_age_hours)
    result: list[Article] = []
    for article in articles:
        if not article.title.strip() or not article.url.strip():
            continue
        published = article.published
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        if published < cutoff:
            continue
        result.append(article)
    return result


def deduplicate_batch(
    articles: list[Article],
    *,
    similarity_threshold: float = TITLE_SIMILARITY_THRESHOLD,
) -> list[tuple[Article, int]]:
    kept: list[tuple[Article, str, set[str]]] = []
    seen_ids: dict[str, int] = {}

    for article in articles:
        if article.id in seen_ids:
            idx = seen_ids[article.id]
            existing, norm_title, sources = kept[idx]
            sources.add(article.source)
            if (article.score > existing.score) or (not existing.summary and article.summary):
                kept[idx] = (article, norm_title, sources)
            continue

        norm_title = _normalize_title(article.title)
        matched_idx: int | None = None
        for idx, (existing, existing_norm, sources) in enumerate(kept):
            ratio = difflib.SequenceMatcher(None, norm_title, existing_norm).ratio()
            if ratio > similarity_threshold:
                matched_idx = idx
                break

        if matched_idx is not None:
            existing, existing_norm, sources = kept[matched_idx]
            sources.add(article.source)
            if article.score > existing.score:
                kept[matched_idx] = (article, norm_title, sources)
        else:
            seen_ids[article.id] = len(kept)
            kept.append((article, norm_title, {article.source}))

    return [(article, len(sources)) for article, _, sources in kept]


def rank_and_diversify(
    deduped: list[tuple[Article, int]],
    *,
    max_articles: int,
    max_age_hours: int,
    now: datetime | None = None,
    soft_max_per_source: int = 2,
    hard_max_per_source: int = 3,
) -> list[Article]:
    reference = now or datetime.now(timezone.utc)
    scored: list[tuple[float, Article]] = []

    for article, source_count in deduped:
        published = article.published
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        age_hours = max(0.0, (reference - published).total_seconds() / 3600.0)
        recency_points = max(0.0, (max_age_hours - age_hours) * 10.0)
        multi_source_bonus = (source_count - 1) * 120.0
        hn_bonus = min(article.score, 600) * 0.5 if article.score > 0 else 0.0
        total_score = recency_points + multi_source_bonus + hn_bonus
        scored.append((total_score, article))

    scored.sort(key=lambda item: (item[0], item[1].published), reverse=True)

    selected: list[Article] = []
    selected_ids: set[str] = set()
    per_source: dict[str, int] = {}

    for limit in (soft_max_per_source, hard_max_per_source):
        for _, article in scored:
            if len(selected) >= max_articles:
                break
            if article.id in selected_ids:
                continue
            count = per_source.get(article.source, 0)
            if count >= limit:
                continue
            selected.append(article)
            selected_ids.add(article.id)
            per_source[article.source] = count + 1
        if len(selected) >= max_articles:
            break

    return selected


async def collect_from_sources(
    feeds_config: FeedsConfig,
    *,
    client: httpx.AsyncClient | None = None,
    per_source_timeout: float = DEFAULT_TIMEOUT,
) -> list[Article]:
    if feeds_config.total_sources == 0:
        return []

    owns_client = client is None
    http_client = client or build_http_client(timeout=per_source_timeout)

    tasks: list[tuple[str, Any]] = []
    for feed in feeds_config.rss:
        coro = asyncio.wait_for(
            fetch_rss_feed(http_client, feed.name, feed.url, lang=feed.lang, category=feed.category),
            timeout=per_source_timeout,
        )
        tasks.append((feed.name, coro))

    if feeds_config.hackernews.enabled:
        coro = asyncio.wait_for(
            fetch_hackernews(
                http_client,
                top_n=feeds_config.hackernews.top_n,
                min_score=feeds_config.hackernews.min_score,
                category=feeds_config.hackernews.category,
            ),
            timeout=per_source_timeout,
        )
        tasks.append(("Hacker News", coro))

    try:
        results = await asyncio.gather(*(coro for _, coro in tasks), return_exceptions=True)
    finally:
        if owns_client:
            await http_client.aclose()

    collected: list[Article] = []
    failures = 0
    for (source_name, _), result in zip(tasks, results):
        if isinstance(result, BaseException):
            failures += 1
            logger.warning("Falha na fonte %s: %s", source_name, result)
        else:
            collected.extend(result)

    if failures == len(tasks):
        raise AllSourcesFailedError(f"Todas as {failures} fontes falharam durante a coleta.")

    return collected


def article_to_feed_item(article: Article, rank: int) -> ValidatedNewsItem:
    clean_title = sanitize_and_truncate(article.title, TITLE_MAX_LEN)
    summary = sanitize_and_truncate(article.summary, min(180, SUMMARY_MAX_LEN))
    raw_cat = article.category if article.category in _VALID_FEED_CATEGORIES else "tech"
    category: Category = promote_critical_category(raw_cat, clean_title, summary)  # type: ignore[arg-type]
    lang = "PT-BR" if article.lang.lower().startswith("pt") else "EN"
    return ValidatedNewsItem(
        rank=rank,
        title=clean_title,
        summary=summary,
        why_it_matters="",
        category=category,
        lang=lang,  # type: ignore[arg-type]
        source_name=sanitize_and_truncate(article.source, 80) or "Fonte",
        url=normalize_url(article.url),
        published_at=article.published.isoformat(),
        confidence="média",
        corroborating_sources=[],
        image_url=article.image_url,
        origin="feed",
    )


def _articles_to_digest(articles: list[Article], date_str: str) -> ValidatedDigest:
    items = [article_to_feed_item(article, rank=idx) for idx, article in enumerate(articles, start=1)]
    return ValidatedDigest(
        date=date_str,
        intro="",
        items=items,
    )


def _top_up_with_rss(
    digest: ValidatedDigest,
    rss_articles: list[Article],
    *,
    target_n: int,
) -> ValidatedDigest:
    existing_ids = {make_article_id(item.url) for item in digest.items}
    existing_norm_titles = [_normalize_title(item.title) for item in digest.items]

    merged = list(digest.items)
    for article in rss_articles:
        if len(merged) >= target_n:
            break
        art_id = make_article_id(article.url)
        if art_id in existing_ids:
            continue
        norm_title = _normalize_title(article.title)
        if any(
            difflib.SequenceMatcher(None, norm_title, prev).ratio() > TITLE_SIMILARITY_THRESHOLD
            for prev in existing_norm_titles
        ):
            continue
        existing_ids.add(art_id)
        existing_norm_titles.append(norm_title)
        merged.append(article_to_feed_item(article, rank=len(merged) + 1))

    return ValidatedDigest(date=digest.date, intro=digest.intro, items=merged)


async def _collect_rss_candidates(
    config: Config,
    feeds_config: FeedsConfig,
    dedup_store: DedupStore,
    *,
    client: httpx.AsyncClient | None = None,
    now: datetime | None = None,
) -> tuple[list[Article], list[Article]]:
    raw_articles = await collect_from_sources(feeds_config, client=client)
    filtered = filter_articles(raw_articles, max_age_hours=config.max_age_hours, now=now)
    if not filtered:
        return [], []

    sent_ids = await dedup_store.get_sent_ids(a.id for a in filtered)
    unsent = [a for a in filtered if a.id not in sent_ids]
    if not unsent:
        return [], []

    deduped = deduplicate_batch(unsent)
    all_ranked = rank_and_diversify(
        deduped,
        max_articles=max(config.ai_max_candidates, config.ai_top_n, config.max_articles),
        max_age_hours=config.max_age_hours,
        now=now,
        soft_max_per_source=4,
        hard_max_per_source=6,
    )
    top_rss = rank_and_diversify(
        deduped,
        max_articles=config.max_articles,
        max_age_hours=config.max_age_hours,
        now=now,
    )
    return all_ranked, top_rss


async def run_digest_pipeline(
    config: Config,
    feeds_config: FeedsConfig,
    dedup_store: DedupStore,
    *,
    curator: CuratorProtocol | None = None,
    client: httpx.AsyncClient | None = None,
    validation_client: httpx.AsyncClient | None = None,
    now: datetime | None = None,
) -> tuple[ValidatedDigest | None, str]:
    reference = now or datetime.now(timezone.utc)
    local_date = reference.astimezone(config.timezone).strftime("%Y-%m-%d")

    rss_cache: tuple[list[Article], list[Article]] | None = None

    async def get_rss() -> tuple[list[Article], list[Article]]:
        nonlocal rss_cache
        if rss_cache is None:
            rss_cache = await _collect_rss_candidates(
                config,
                feeds_config,
                dedup_store,
                client=client,
                now=reference,
            )
        return rss_cache

    if config.ai_mode != "off" and curator is not None:
        sent_ids = await dedup_store.get_all_sent_ids()
        recent_titles = await dedup_store.get_recent_titles(days=config.already_sent_days, limit=100)

        if config.ai_mode == "auto":
            attempts = ["grounded", "candidates"]
        elif config.ai_mode == "grounded":
            attempts = ["grounded"]
        else:
            attempts = ["candidates"]

        for sub_mode in attempts:
            candidates_for_mode: list[Article] | None = None
            candidate_urls: set[str] | None = None

            if sub_mode == "candidates":
                try:
                    candidates_for_mode, _ = await get_rss()
                except AllSourcesFailedError:
                    logger.warning("Coleta RSS falhou antes do Modo B.")
                    candidates_for_mode = []
                if not candidates_for_mode:
                    logger.info("Sem candidatos RSS disponiveis para Modo B.")
                    continue
                candidate_urls = {a.url for a in candidates_for_mode[: config.ai_max_candidates]}

            raw_digest, mode_used = await curator.curate(
                sub_mode,
                config.ai_top_n,
                recent_titles,
                candidates_for_mode,
                date_str=local_date,
                already_sent_days=config.already_sent_days,
                max_candidates=config.ai_max_candidates,
                timezone_name=config.timezone_name,
            )

            if raw_digest is None or mode_used == "C":
                continue

            validated = await validate_digest(
                raw_digest,
                mode_used=mode_used,
                sent_ids=sent_ids,
                already_sent_titles=recent_titles,
                max_age_hours=config.ai_max_age_hours,
                candidate_urls=candidate_urls,
                grounding_domains=getattr(curator, "last_grounding_domains", None),
                client=validation_client,
                now=reference,
            )

            if not validated.items:
                logger.warning(
                    "Digest do Modo %s ficou vazio apos validacao; descendo um degrau.",
                    mode_used,
                )
                continue

            if len(validated.items) < config.ai_min_items:
                logger.info(
                    "Modo %s aprovou %d itens (< %d); completando com RSS.",
                    mode_used,
                    len(validated.items),
                    config.ai_min_items,
                )
                try:
                    _, top_rss = await get_rss()
                    validated = _top_up_with_rss(validated, top_rss, target_n=config.ai_top_n)
                except AllSourcesFailedError:
                    logger.warning("Nao foi possivel completar com RSS (todas as fontes falharam).")

            return validated, mode_used

    _, top_rss = await get_rss()
    if not top_rss:
        return None, "C"

    logger.info("Pipeline operando no Modo C (RSS puro): %d itens selecionados.", len(top_rss))
    return _articles_to_digest(top_rss, date_str=local_date), "C"
