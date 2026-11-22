from __future__ import annotations

import asyncio
import difflib
import ipaddress
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from bot.ai_schema import (
    INTRO_MAX_LEN,
    SUMMARY_MAX_LEN,
    TITLE_MAX_LEN,
    WHY_IT_MATTERS_MAX_LEN,
    Category,
    Digest,
    NewsItem,
    Source,
    ValidatedDigest,
    ValidatedNewsItem,
)
from bot.fetchers import USER_AGENT
from bot.models import clean_html, make_article_id, normalize_url

logger = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"@(everyone|here)|<@[!&]?\d+>")
_TITLE_NORM_RE = re.compile(r"[^a-z0-9\s]+")
_CRITICAL_URGENCY_RE = re.compile(
    r"\b(zero[- ]?day|0[- ]?day|rce|remote code execution|critical\s+vulnerabilit|actively\s+exploited|cve-202\d-\d{4,})\b",
    re.IGNORECASE,
)

URL_SHORTENERS = {
    "bit.ly",
    "t.co",
    "tinyurl.com",
    "goo.gl",
    "ow.ly",
    "is.gd",
    "buff.ly",
    "rebrand.ly",
    "shorturl.at",
    "rb.gy",
    "cutt.ly",
    "tiny.cc",
    "lnkd.in",
    "ift.tt",
    "dlvr.it",
}

VALIDATION_TIMEOUT = 10.0
VALIDATION_MAX_REDIRECTS = 5
VALIDATION_CONCURRENCY = 3
TITLE_SIMILARITY_THRESHOLD = 0.85


def promote_critical_category(category: Category, title: str, summary: str = "") -> Category:
    if category == "sec" and _CRITICAL_URGENCY_RE.search(f"{title} {summary}"):
        return "critico"
    return category


def sanitize_and_truncate(text: str | None, limit: int) -> str:
    cleaned = clean_html(text) or ""
    cleaned = _MENTION_RE.sub(lambda m: m.group(0).replace("@", "@\u200b"), cleaned)
    if len(cleaned) <= limit:
        return cleaned
    if limit <= 3:
        return cleaned[:limit]
    return cleaned[: limit - 3].rstrip() + "..."


def _normalize_title(title: str) -> str:
    cleaned = _TITLE_NORM_RE.sub(" ", title.lower())
    return " ".join(cleaned.split())


def is_allowed_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False

    if parts.scheme.lower() != "https":
        return False

    host = (parts.hostname or "").lower().strip(".")
    if not host or "." not in host:
        return False

    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass

    if host in URL_SHORTENERS or any(host.endswith("." + s) for s in URL_SHORTENERS):
        return False

    return True


def extract_og_image(html_content: str, base_url: str) -> str | None:
    if not html_content:
        return None
    try:
        soup = BeautifulSoup(html_content[:150_000], "html.parser")
        for attr_name, attr_val in (("property", "og:image"), ("name", "twitter:image")):
            tag = soup.find("meta", attrs={attr_name: attr_val})
            if tag and tag.get("content"):
                raw = str(tag["content"]).strip()
                resolved = urljoin(base_url, raw)
                if resolved.startswith(("http://", "https://")):
                    return resolved
    except Exception:
        return None
    return None


def parse_published_at(raw: str | None) -> datetime | None:
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def _check_url(
    client: httpx.AsyncClient,
    url: str,
    confidence: str,
    semaphore: asyncio.Semaphore,
) -> tuple[bool, str, str | None]:
    normalized = normalize_url(url)
    if not is_allowed_url(normalized):
        logger.debug("URL rejeitada na pre-validacao: %s", normalized)
        return False, normalized, None

    async with semaphore:
        try:
            response = await client.get(normalized)
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            logger.debug("Falha HTTP/DNS/SSL ao validar %s: %s", normalized, exc)
            return False, normalized, None

    final_url = normalize_url(str(response.url))
    if not is_allowed_url(final_url):
        logger.debug("URL final apos redirect rejeitada: %s", final_url)
        return False, final_url, None

    status = response.status_code
    if 200 <= status < 300:
        content_type = (response.headers.get("content-type") or "").lower()
        image_url = None
        if "html" in content_type or not content_type:
            image_url = extract_og_image(response.text, final_url)
        return True, final_url, image_url

    if status in (401, 403, 429):
        if confidence != "baixa":
            logger.info(
                "Validacao inconclusiva (HTTP %d) para %s; mantido pois confidence=%s.",
                status,
                final_url,
                confidence,
            )
            return True, final_url, None
        logger.info(
            "Validacao inconclusiva (HTTP %d) para %s; descartado pois confidence=baixa.",
            status,
            final_url,
        )
        return False, final_url, None

    logger.debug("URL descartada (HTTP %d): %s", status, final_url)
    return False, final_url, None


async def validate_digest(
    digest: Digest,
    *,
    mode_used: str,
    sent_ids: set[str],
    already_sent_titles: Iterable[str],
    max_age_hours: int = 48,
    candidate_urls: set[str] | None = None,
    grounding_domains: set[str] | None = None,
    client: httpx.AsyncClient | None = None,
    now: datetime | None = None,
) -> ValidatedDigest:
    reference = now or datetime.now(timezone.utc)
    cutoff = reference - timedelta(hours=max_age_hours)

    normalized_candidate_urls: set[str] | None = None
    if mode_used == "B" and candidate_urls is not None:
        normalized_candidate_urls = {normalize_url(u) for u in candidate_urls}

    owns_client = client is None
    http_client = client or httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(VALIDATION_TIMEOUT),
        follow_redirects=True,
        max_redirects=VALIDATION_MAX_REDIRECTS,
    )
    semaphore = asyncio.Semaphore(VALIDATION_CONCURRENCY)

    ordered_items = sorted(digest.items, key=lambda item: item.rank)

    pre_filtered: list[tuple[NewsItem, str | None]] = []
    for item in ordered_items:
        if mode_used == "B" and normalized_candidate_urls is not None:
            raw_ok = item.url in (candidate_urls or set())
            norm_ok = normalize_url(item.url) in normalized_candidate_urls
            if not (raw_ok or norm_ok):
                logger.info("Modo B: URL fora da lista de candidatos descartada: %s", item.url)
                continue

        parsed_dt = parse_published_at(item.published_at)
        if parsed_dt is not None and parsed_dt < cutoff:
            logger.debug("Item descartado por idade (%s): %s", item.published_at, item.title)
            continue
        clean_pub = parsed_dt.isoformat() if parsed_dt is not None else None
        pre_filtered.append((item, clean_pub))

    try:
        url_checks = await asyncio.gather(
            *(
                _check_url(http_client, item.url, item.confidence, semaphore)
                for item, _ in pre_filtered
            )
        )
    finally:
        if owns_client:
            await http_client.aclose()

    sent_norm_titles = [_normalize_title(t) for t in already_sent_titles if t and t.strip()]
    kept_items: list[ValidatedNewsItem] = []
    seen_item_ids: set[str] = set()
    kept_norm_titles: list[str] = []

    for (item, clean_pub), (approved, canonical_url, image_url) in zip(pre_filtered, url_checks):
        if not approved:
            continue

        canonical_id = make_article_id(canonical_url)
        if canonical_id in sent_ids or canonical_id in seen_item_ids:
            logger.debug("Item descartado por URL duplicada: %s", canonical_url)
            continue

        corrob_duplicate = False
        cleaned_corrob: list[Source] = []
        for src in item.corroborating_sources:
            norm_src_url = normalize_url(src.url)
            if make_article_id(norm_src_url) in sent_ids:
                corrob_duplicate = True
                break
            if is_allowed_url(norm_src_url):
                cleaned_corrob.append(
                    Source(
                        name=sanitize_and_truncate(src.name, 80),
                        url=norm_src_url,
                    )
                )
        if corrob_duplicate:
            logger.debug("Item descartado pois corroborating_source ja foi enviada: %s", item.title)
            continue

        clean_title = sanitize_and_truncate(item.title, TITLE_MAX_LEN)
        if not clean_title:
            continue

        norm_title = _normalize_title(clean_title)
        if any(
            difflib.SequenceMatcher(None, norm_title, prev).ratio() > TITLE_SIMILARITY_THRESHOLD
            for prev in sent_norm_titles
        ):
            logger.debug("Item descartado por similaridade com titulo ja enviado: %s", clean_title)
            continue

        if any(
            difflib.SequenceMatcher(None, norm_title, prev).ratio() > TITLE_SIMILARITY_THRESHOLD
            for prev in kept_norm_titles
        ):
            logger.debug("Item descartado por duplicata dentro do mesmo digest: %s", clean_title)
            continue

        clean_summary = sanitize_and_truncate(item.summary, SUMMARY_MAX_LEN)
        clean_why = sanitize_and_truncate(item.why_it_matters, WHY_IT_MATTERS_MAX_LEN)
        clean_source = sanitize_and_truncate(item.source_name, 80) or "Fonte"
        final_category = promote_critical_category(item.category, clean_title, clean_summary)

        seen_item_ids.add(canonical_id)
        kept_norm_titles.append(norm_title)
        kept_items.append(
            ValidatedNewsItem(
                rank=len(kept_items) + 1,
                title=clean_title,
                summary=clean_summary,
                why_it_matters=clean_why,
                category=final_category,
                lang=item.lang,
                source_name=clean_source,
                url=canonical_url,
                published_at=clean_pub,
                confidence=item.confidence,
                corroborating_sources=cleaned_corrob,
                image_url=image_url,
                origin="ai",
            )
        )

    if grounding_domains and kept_items:
        matched = sum(
            1
            for it in kept_items
            if urlsplit(it.url).netloc.lower() in grounding_domains
        )
        logger.info(
            "Sinal extra grounding_chunks: %d/%d itens aprovados com dominio presente nos chunks.",
            matched,
            len(kept_items),
        )

    logger.info(
        "Validacao concluida (Modo %s): %d/%d itens aprovados.",
        mode_used,
        len(kept_items),
        len(digest.items),
    )

    return ValidatedDigest(
        date=digest.date,
        intro=sanitize_and_truncate(digest.intro, INTRO_MAX_LEN),
        items=kept_items,
    )
