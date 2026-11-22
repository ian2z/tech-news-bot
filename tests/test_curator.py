from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import errors as genai_errors

from bot.curator import GeminiCurator, extract_json_from_text
from bot.models import Article, make_article_id


def _sample_digest_dict(category: str = "ai") -> dict[str, Any]:
    return {
        "date": "2026-11-22",
        "intro": "Dia movimentado em chips e modelos abertos.",
        "items": [
            {
                "rank": 1,
                "title": "Novo chip ARM64 reduz consumo em servidores domesticos",
                "summary": "Fabricante anunciou chip com 30% menos consumo. Primeiras placas chegam em dezembro.",
                "why_it_matters": "Torna homelabs mais baratos e silenciosos.",
                "category": category,
                "lang": "EN",
                "source_name": "TechCrunch",
                "url": "https://techcrunch.com/2026/11/22/arm64-chip",
                "published_at": "2026-11-22T09:00:00-03:00",
                "confidence": "alta",
                "corroborating_sources": [
                    {"name": "Ars Technica", "url": "https://arstechnica.com/arm64-chip"}
                ],
            }
        ],
    }


def _sample_candidate() -> Article:
    url = "https://techcrunch.com/2026/11/22/arm64-chip"
    return Article(
        id=make_article_id(url),
        title="New ARM64 chip cuts power usage",
        url=url,
        source="TechCrunch",
        published=datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc),
        summary="Maker announced a new chip.",
    )


class _FakeModels:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self._responses:
            raise RuntimeError("No more fake responses")
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(text=outcome, candidates=[])


def _fake_client(responses: list[Any]) -> tuple[Any, _FakeModels]:
    models = _FakeModels(responses)
    client = SimpleNamespace(aio=SimpleNamespace(models=models))
    return client, models


def _make_api_error(code: int, message: str) -> genai_errors.APIError:
    return genai_errors.APIError(code, {"error": {"code": code, "message": message}})


def test_extract_json_from_text_strips_markdown_fences() -> None:
    raw = 'Aqui esta o briefing:\n```json\n{"date": "2026-11-22", "intro": "ok", "items": []}\n```\nFim.'
    extracted = extract_json_from_text(raw)
    parsed = json.loads(extracted)
    assert parsed["date"] == "2026-11-22"


@pytest.mark.asyncio
async def test_curator_mode_a_valid_digest() -> None:
    payload = json.dumps(_sample_digest_dict(), ensure_ascii=False)
    client, models = _fake_client([payload])
    curator = GeminiCurator(api_key="test", client=client, max_retries=2)

    digest, mode_used = await curator.curate(
        "auto", 8, [], [_sample_candidate()], date_str="2026-11-22"
    )
    assert mode_used == "A"
    assert digest is not None
    assert len(digest.items) == 1
    assert len(models.calls) == 1


@pytest.mark.asyncio
async def test_curator_falls_back_from_a_to_a_prime_on_schema_combo_400() -> None:
    err_400 = _make_api_error(400, "Controlled generation with response_schema is not supported with google_search")
    wrapped_json = "Segue:\n```json\n" + json.dumps(_sample_digest_dict(), ensure_ascii=False) + "\n```"
    client, models = _fake_client([err_400, wrapped_json])
    curator = GeminiCurator(api_key="test", client=client, max_retries=2)

    digest, mode_used = await curator.curate(
        "auto", 8, [], [_sample_candidate()], date_str="2026-11-22"
    )
    assert mode_used == "A'"
    assert digest is not None
    assert len(models.calls) == 2


@pytest.mark.asyncio
async def test_curator_falls_back_to_mode_b_on_grounding_403() -> None:
    err_403 = _make_api_error(403, "google_search grounding tool is not available on this plan")
    payload_b = json.dumps(_sample_digest_dict(), ensure_ascii=False)
    client, models = _fake_client([err_403, payload_b])
    curator = GeminiCurator(api_key="test", client=client, max_retries=2)

    digest, mode_used = await curator.curate(
        "auto", 8, [], [_sample_candidate()], date_str="2026-11-22"
    )
    assert mode_used == "B"
    assert digest is not None
    # Should skip A' directly because grounding was marked unavailable
    assert len(models.calls) == 2


@pytest.mark.asyncio
async def test_curator_steps_down_on_invalid_category_and_reaches_mode_c(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("bot.curator.asyncio.sleep", _no_sleep)

    bad_category = json.dumps(_sample_digest_dict(category="CategoriaInexistente"), ensure_ascii=False)
    err_429 = _make_api_error(429, "Resource exhausted")
    # Mode A: 2 parse failures; Mode A': 2 429s; Mode B: 2 invalid JSONs
    client, _ = _fake_client([bad_category, bad_category, err_429, err_429, "not json", "still not json"])
    curator = GeminiCurator(api_key="test", client=client, max_retries=2)

    digest, mode_used = await curator.curate(
        "auto", 8, [], [_sample_candidate()], date_str="2026-11-22"
    )
    assert digest is None
    assert mode_used == "C"
