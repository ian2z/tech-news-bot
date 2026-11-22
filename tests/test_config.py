from __future__ import annotations

import pytest

from bot.config import ConfigError, load_config


def _base_env(**overrides: str) -> dict[str, str]:
    env = {
        "DISCORD_TOKEN": "fake-discord-token-123",
        "DISCORD_CHANNEL_ID": "1234567890",
        "DISCORD_GUILD_ID": "9876543210",
        "POST_TIME": "08:30",
        "TIMEZONE": "America/Fortaleza",
        "MAX_ARTICLES": "8",
        "MAX_AGE_HOURS": "30",
        "DB_PATH": "data/sent.db",
        "LOG_LEVEL": "INFO",
        "AI_MODE": "off",
    }
    env.update(overrides)
    return env


def test_load_config_valid_with_ai_off_without_key() -> None:
    cfg = load_config(env=_base_env(AI_MODE="off"), load_dotenv_file=False)
    assert cfg.discord_channel_id == 1234567890
    assert cfg.discord_guild_id == 9876543210
    assert cfg.post_hour == 8
    assert cfg.post_minute == 30
    assert cfg.timezone_name == "America/Fortaleza"
    assert cfg.ai_mode == "off"
    assert cfg.gemini_api_key is None
    assert "fake-discord-token-123" not in repr(cfg)


def test_load_config_rejects_invalid_post_time() -> None:
    for bad_time in ("25:00", "08:60", "8:30", "invalid", "12:3"):
        with pytest.raises(ConfigError, match="POST_TIME"):
            load_config(env=_base_env(POST_TIME=bad_time), load_dotenv_file=False)


def test_load_config_rejects_invalid_timezone() -> None:
    with pytest.raises(ConfigError, match="TIMEZONE"):
        load_config(env=_base_env(TIMEZONE="Mars/Olympus_Mons"), load_dotenv_file=False)


def test_load_config_requires_gemini_key_when_ai_enabled() -> None:
    for mode in ("auto", "grounded", "candidates"):
        with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
            load_config(env=_base_env(AI_MODE=mode, GEMINI_API_KEY=""), load_dotenv_file=False)

    cfg = load_config(
        env=_base_env(AI_MODE="auto", GEMINI_API_KEY="secret-gemini-key"),
        load_dotenv_file=False,
    )
    assert cfg.ai_mode == "auto"
    assert cfg.gemini_api_key == "secret-gemini-key"
    assert "secret-gemini-key" not in repr(cfg)


def test_load_config_validates_ai_mode_and_top_n() -> None:
    with pytest.raises(ConfigError, match="AI_MODE"):
        load_config(env=_base_env(AI_MODE="turbo"), load_dotenv_file=False)

    with pytest.raises(ConfigError, match="AI_TOP_N"):
        load_config(env=_base_env(AI_TOP_N="21"), load_dotenv_file=False)

    with pytest.raises(ConfigError, match="AI_TOP_N"):
        load_config(env=_base_env(AI_TOP_N="0"), load_dotenv_file=False)


def test_load_config_rejects_non_int_ids() -> None:
    with pytest.raises(ConfigError, match="DISCORD_CHANNEL_ID"):
        load_config(env=_base_env(DISCORD_CHANNEL_ID="not-an-int"), load_dotenv_file=False)

    with pytest.raises(ConfigError, match="DISCORD_GUILD_ID"):
        load_config(env=_base_env(DISCORD_GUILD_ID="abc"), load_dotenv_file=False)
