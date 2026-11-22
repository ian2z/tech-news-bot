from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import aiosqlite
from pydantic import ValidationError

from bot.ai_schema import ValidatedDigest
from bot.models import Article, make_article_id, normalize_url

logger = logging.getLogger(__name__)

_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS sent_articles (
    id        TEXT PRIMARY KEY,
    url       TEXT NOT NULL,
    title     TEXT NOT NULL,
    source    TEXT NOT NULL,
    sent_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sent_at ON sent_articles(sent_at);

CREATE TABLE IF NOT EXISTS daily_digest (
    date        TEXT PRIMARY KEY,
    mode_used   TEXT NOT NULL,
    digest_json TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    posted_at   TEXT
);
"""


@dataclass(frozen=True)
class CachedDigest:
    date: str
    mode_used: str
    digest: ValidatedDigest
    created_at: datetime
    posted_at: datetime | None


def _parse_iso_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class DedupStore:
    def __init__(
        self,
        db_path: str,
        retention_days: int = 60,
        digest_retention_days: int = 30,
    ) -> None:
        self.db_path = Path(db_path)
        self.retention_days = retention_days
        self.digest_retention_days = digest_retention_days

    async def init(self) -> int:
        if self.db_path != Path(":memory:"):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        now_utc = datetime.now(timezone.utc)
        sent_cutoff = (now_utc - timedelta(days=self.retention_days)).isoformat()
        digest_cutoff = (now_utc - timedelta(days=self.digest_retention_days)).isoformat()

        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_CREATE_TABLES_SQL)
            c1 = await db.execute("DELETE FROM sent_articles WHERE sent_at < ?", (sent_cutoff,))
            deleted_sent = c1.rowcount if c1.rowcount is not None and c1.rowcount > 0 else 0
            c2 = await db.execute("DELETE FROM daily_digest WHERE created_at < ?", (digest_cutoff,))
            deleted_digests = c2.rowcount if c2.rowcount is not None and c2.rowcount > 0 else 0
            await db.commit()

        if deleted_sent or deleted_digests:
            logger.info(
                "Limpeza SQLite: %d artigos e %d digests antigos removidos.",
                deleted_sent,
                deleted_digests,
            )
        return deleted_sent

    async def get_sent_ids(self, article_ids: Iterable[str]) -> set[str]:
        ids = list(dict.fromkeys(article_ids))
        if not ids:
            return set()

        sent: set[str] = set()
        async with aiosqlite.connect(self.db_path) as db:
            chunk_size = 500
            for i in range(0, len(ids), chunk_size):
                chunk = ids[i : i + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                async with db.execute(
                    f"SELECT id FROM sent_articles WHERE id IN ({placeholders})",
                    chunk,
                ) as cursor:
                    async for row in cursor:
                        sent.add(row[0])
        return sent

    async def get_all_sent_ids(self) -> set[str]:
        sent: set[str] = set()
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT id FROM sent_articles") as cursor:
                async for row in cursor:
                    sent.add(row[0])
        return sent

    async def get_recent_titles(self, days: int = 3, limit: int = 100) -> list[str]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        titles: list[str] = []
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT DISTINCT title FROM sent_articles WHERE sent_at >= ? ORDER BY sent_at DESC LIMIT ?",
                (cutoff, limit),
            ) as cursor:
                async for row in cursor:
                    titles.append(row[0])
        return titles

    async def mark_sent(self, articles: Iterable[Article]) -> int:
        now_iso = datetime.now(timezone.utc).isoformat()
        rows = [(a.id, a.url, a.title, a.source, now_iso) for a in articles]
        if not rows:
            return 0

        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                "INSERT OR IGNORE INTO sent_articles (id, url, title, source, sent_at) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            await db.commit()
        return len(rows)

    async def mark_digest_sent(self, date_str: str, digest: ValidatedDigest) -> int:
        now_iso = datetime.now(timezone.utc).isoformat()
        rows: list[tuple[str, str, str, str, str]] = []
        for item in digest.items:
            canonical_url = normalize_url(item.url)
            rows.append(
                (make_article_id(canonical_url), canonical_url, item.title, item.source_name, now_iso)
            )
            for src in item.corroborating_sources:
                src_url = normalize_url(src.url)
                rows.append(
                    (make_article_id(src_url), src_url, item.title, src.name or item.source_name, now_iso)
                )

        async with aiosqlite.connect(self.db_path) as db:
            if rows:
                await db.executemany(
                    "INSERT OR IGNORE INTO sent_articles (id, url, title, source, sent_at) VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
            await db.execute(
                "UPDATE daily_digest SET posted_at = ? WHERE date = ?",
                (now_iso, date_str),
            )
            await db.commit()
        return len(rows)

    async def save_digest(
        self,
        date_str: str,
        mode_used: str,
        digest: ValidatedDigest,
        *,
        created_at: datetime | None = None,
    ) -> CachedDigest:
        created = (created_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        created_iso = created.isoformat()
        payload = digest.model_dump_json()

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO daily_digest (date, mode_used, digest_json, created_at, posted_at)
                VALUES (?, ?, ?, ?, NULL)
                ON CONFLICT(date) DO UPDATE SET
                    mode_used = excluded.mode_used,
                    digest_json = excluded.digest_json,
                    created_at = excluded.created_at,
                    posted_at = NULL
                """,
                (date_str, mode_used, payload, created_iso),
            )
            await db.commit()

        return CachedDigest(
            date=date_str,
            mode_used=mode_used,
            digest=digest,
            created_at=created,
            posted_at=None,
        )

    def _row_to_cached_digest(self, row: aiosqlite.Row | tuple) -> CachedDigest | None:
        try:
            digest = ValidatedDigest.model_validate_json(row[2])
        except ValidationError as exc:
            logger.warning(
                "Cache daily_digest de %s ignorado por schema incompativel: %s",
                row[0],
                exc.error_count(),
            )
            return None
        return CachedDigest(
            date=row[0],
            mode_used=row[1],
            digest=digest,
            created_at=_parse_iso_utc(row[3]) or datetime.now(timezone.utc),
            posted_at=_parse_iso_utc(row[4]),
        )

    async def get_digest(self, date_str: str) -> CachedDigest | None:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT date, mode_used, digest_json, created_at, posted_at FROM daily_digest WHERE date = ?",
                (date_str,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_cached_digest(row)

    async def get_latest_digest(self) -> CachedDigest | None:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT date, mode_used, digest_json, created_at, posted_at "
                "FROM daily_digest ORDER BY created_at DESC LIMIT 1"
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_cached_digest(row)
