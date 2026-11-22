from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_WHITESPACE_RE = re.compile(r"\s+")


class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return " ".join(self._parts)


def clean_html(text: str | None) -> str | None:
    if not text:
        return None
    stripper = _HTMLStripper()
    stripper.feed(text)
    stripper.close()
    cleaned = html.unescape(stripper.get_text())
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    return cleaned or None


def normalize_url(url: str) -> str:
    raw = url.strip()
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/")

    filtered_query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() != "ref"
    ]
    query = urlencode(filtered_query, doseq=True)

    return urlunsplit((scheme, netloc, path, query, ""))


def make_article_id(url: str) -> str:
    normalized = normalize_url(url)
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


@dataclass
class Article:
    id: str
    title: str
    url: str
    source: str
    published: datetime
    summary: str | None = None
    image_url: str | None = None
    score: int = 0
    ai_summary: str | None = None
    lang: str = "en"
    category: str = "tech"
