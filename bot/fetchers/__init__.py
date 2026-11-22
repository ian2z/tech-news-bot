from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

USER_AGENT = "TechNewsBot/1.0 (+https://github.com/tech-news-bot; RSS/API reader)"
DEFAULT_TIMEOUT = 15.0


def build_http_client(timeout: float = DEFAULT_TIMEOUT) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(timeout),
        follow_redirects=True,
    )


async def fetch_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    retries: int = 3,
    base_delay: float = 1.0,
) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = await client.get(url)
            response.raise_for_status()
            return response
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            last_exc = exc
            if attempt < retries:
                delay = base_delay * (2 ** (attempt - 1))
                logger.debug(
                    "Tentativa %d/%d falhou para %s (%s). Tentando novamente em %.1fs.",
                    attempt,
                    retries,
                    url,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc
