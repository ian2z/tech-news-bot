from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import time
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_VALID_AI_MODES = {"auto", "grounded", "candidates", "off"}


class ConfigError(ValueError):
    """Raised when environment configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    discord_token: str = field(repr=False)
    discord_channel_id: int
    discord_guild_id: int
    post_hour: int
    post_minute: int
    timezone_name: str
    timezone: ZoneInfo
    max_articles: int = 12
    max_age_hours: int = 24
    db_path: str = "data/sent.db"
    feeds_path: str = "feeds.yaml"
    log_level: str = "INFO"
    ai_mode: str = "candidates"
    gemini_api_key: str | None = field(default=None, repr=False)
    gemini_model: str = "gemini-3.5-flash-lite"
    ai_top_n: int = 12
    ai_min_items: int = 4
    ai_max_candidates: int = 50
    already_sent_days: int = 3
    ai_timeout_seconds: int = 90
    ai_max_retries: int = 3
    ai_max_age_hours: int = 48
    manual_cooldown_minutes: int = 60

    @property
    def post_time(self) -> time:
        return time(hour=self.post_hour, minute=self.post_minute, tzinfo=self.timezone)

    @property
    def post_time_str(self) -> str:
        return f"{self.post_hour:02d}:{self.post_minute:02d}"


def _clean_value(raw: str | None) -> str:
    if raw is None:
        return ""
    return raw.strip()


def _parse_int(
    env: Mapping[str, str],
    name: str,
    *,
    required: bool = True,
    default: int | None = None,
    min_value: int = 1,
    max_value: int | None = None,
) -> int:
    raw = _clean_value(env.get(name))
    if not raw:
        if required:
            raise ConfigError(f"Variavel obrigatoria ausente: {name}")
        assert default is not None
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} deve ser um numero inteiro valido, recebido: {raw!r}") from exc
    if value < min_value:
        raise ConfigError(f"{name} deve ser >= {min_value}, recebido: {value}")
    if max_value is not None and value > max_value:
        raise ConfigError(f"{name} deve ser <= {max_value}, recebido: {value}")
    return value


def load_config(env: Mapping[str, str] | None = None, *, load_dotenv_file: bool = True) -> Config:
    if env is None:
        if load_dotenv_file:
            load_dotenv()
        source_env: Mapping[str, str] = os.environ
    else:
        source_env = env

    discord_token = _clean_value(source_env.get("DISCORD_TOKEN"))
    if not discord_token:
        raise ConfigError("Variavel obrigatoria ausente: DISCORD_TOKEN")

    discord_channel_id = _parse_int(source_env, "DISCORD_CHANNEL_ID", required=True, min_value=1)
    discord_guild_id = _parse_int(source_env, "DISCORD_GUILD_ID", required=True, min_value=1)

    post_time_raw = _clean_value(source_env.get("POST_TIME")) or "08:00"
    match = _TIME_RE.match(post_time_raw)
    if not match:
        raise ConfigError(f"POST_TIME invalido (use HH:MM em formato 24h): {post_time_raw!r}")
    post_hour = int(match.group(1))
    post_minute = int(match.group(2))

    timezone_name = _clean_value(source_env.get("TIMEZONE")) or "America/Fortaleza"
    try:
        tz = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, KeyError, ValueError) as exc:
        raise ConfigError(f"TIMEZONE invalido: {timezone_name!r}") from exc

    max_articles = _parse_int(source_env, "MAX_ARTICLES", required=False, default=12, min_value=1, max_value=20)
    max_age_hours = _parse_int(source_env, "MAX_AGE_HOURS", required=False, default=24, min_value=1)
    db_path = _clean_value(source_env.get("DB_PATH")) or "data/sent.db"
    feeds_path = _clean_value(source_env.get("FEEDS_PATH")) or "feeds.yaml"

    log_level = (_clean_value(source_env.get("LOG_LEVEL")) or "INFO").upper()
    if log_level not in _VALID_LOG_LEVELS:
        raise ConfigError(
            f"LOG_LEVEL invalido: {log_level!r}. Valores aceitos: {', '.join(sorted(_VALID_LOG_LEVELS))}"
        )

    ai_mode = (_clean_value(source_env.get("AI_MODE")) or "candidates").lower()
    if ai_mode not in _VALID_AI_MODES:
        raise ConfigError(
            f"AI_MODE invalido: {ai_mode!r}. Valores aceitos: {', '.join(sorted(_VALID_AI_MODES))}"
        )

    gemini_api_key = _clean_value(source_env.get("GEMINI_API_KEY")) or None
    gemini_model = _clean_value(source_env.get("GEMINI_MODEL")) or "gemini-3.5-flash-lite"

    if ai_mode != "off" and not gemini_api_key:
        raise ConfigError("GEMINI_API_KEY e obrigatoria quando AI_MODE != off")

    ai_top_n = _parse_int(source_env, "AI_TOP_N", required=False, default=12, min_value=1, max_value=20)
    ai_min_items = _parse_int(source_env, "AI_MIN_ITEMS", required=False, default=4, min_value=1, max_value=20)
    ai_max_candidates = _parse_int(source_env, "AI_MAX_CANDIDATES", required=False, default=50, min_value=1)
    already_sent_days = _parse_int(source_env, "ALREADY_SENT_DAYS", required=False, default=3, min_value=1)
    ai_timeout_seconds = _parse_int(source_env, "AI_TIMEOUT_SECONDS", required=False, default=90, min_value=5)
    ai_max_retries = _parse_int(source_env, "AI_MAX_RETRIES", required=False, default=3, min_value=1)
    ai_max_age_hours = _parse_int(source_env, "AI_MAX_AGE_HOURS", required=False, default=48, min_value=1)
    manual_cooldown_minutes = _parse_int(
        source_env, "MANUAL_COOLDOWN_MINUTES", required=False, default=60, min_value=0
    )

    return Config(
        discord_token=discord_token,
        discord_channel_id=discord_channel_id,
        discord_guild_id=discord_guild_id,
        post_hour=post_hour,
        post_minute=post_minute,
        timezone_name=timezone_name,
        timezone=tz,
        max_articles=max_articles,
        max_age_hours=max_age_hours,
        db_path=db_path,
        feeds_path=feeds_path,
        log_level=log_level,
        ai_mode=ai_mode,
        gemini_api_key=gemini_api_key,
        gemini_model=gemini_model,
        ai_top_n=ai_top_n,
        ai_min_items=ai_min_items,
        ai_max_candidates=ai_max_candidates,
        already_sent_days=already_sent_days,
        ai_timeout_seconds=ai_timeout_seconds,
        ai_max_retries=ai_max_retries,
        ai_max_age_hours=ai_max_age_hours,
        manual_cooldown_minutes=manual_cooldown_minutes,
    )


def setup_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
