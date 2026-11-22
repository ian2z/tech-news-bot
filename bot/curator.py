from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import urlsplit

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import ValidationError

from bot.ai_schema import Digest
from bot.models import Article
from bot.prompts import (
    SYSTEM_PROMPT_A,
    SYSTEM_PROMPT_A_PRIME,
    SYSTEM_PROMPT_B,
    build_user_prompt_a,
    build_user_prompt_b,
)

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:json)?\s*|\s*```", re.IGNORECASE)


def extract_json_from_text(text: str) -> str:
    stripped = _FENCE_RE.sub("", text or "").strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("Nenhum objeto JSON encontrado na resposta do modelo.")
    return stripped[start : end + 1]


def _extract_grounding_domains(response: Any) -> set[str]:
    domains: set[str] = set()
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return domains
    metadata = getattr(candidates[0], "grounding_metadata", None)
    chunks = getattr(metadata, "grounding_chunks", None) if metadata else None
    if not chunks:
        return domains
    for chunk in chunks:
        web = getattr(chunk, "web", None)
        uri = getattr(web, "uri", None) if web else None
        if uri and isinstance(uri, str):
            host = urlsplit(uri).netloc.lower()
            if host:
                domains.add(host)
    return domains


def _is_schema_grounding_incompatibility(code: int | None, message: str) -> bool:
    if code != 400:
        return False
    lowered = message.lower()
    keywords = (
        "controlled generation",
        "response_schema",
        "response schema",
        "response_mime_type",
        "json mode",
        "structured output",
        "schema",
    )
    return any(k in lowered for k in keywords)


def _is_grounding_unavailable(code: int | None, message: str) -> bool:
    if code not in (400, 403):
        return False
    lowered = message.lower()
    keywords = (
        "google_search",
        "google search",
        "search grounding",
        "grounding",
        "tool",
        "not supported",
        "permission",
        "unavailable",
        "billing",
    )
    return any(k in lowered for k in keywords)


class GeminiCurator:
    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3.8-flash",
        *,
        timeout_seconds: int = 90,
        max_retries: int = 3,
        client: Any | None = None,
    ) -> None:
        self._client = client if client is not None else genai.Client(api_key=api_key)
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.last_grounding_domains: set[str] = set()

    def _allowed_steps(self, mode: str) -> list[str]:
        if mode == "auto":
            return ["A", "A'", "B"]
        if mode == "grounded":
            return ["A", "A'"]
        if mode == "candidates":
            return ["B"]
        return []

    async def _call_model(
        self,
        *,
        step: str,
        system_prompt: str,
        user_prompt: str,
    ) -> tuple[Digest, set[str]]:
        grounded = step in ("A", "A'")
        use_schema = step in ("A", "B")

        config_kwargs: dict[str, Any] = {
            "system_instruction": system_prompt,
            "temperature": 1.0 if grounded else 0.3,
        }
        if grounded:
            config_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]
        if use_schema:
            config_kwargs["response_mime_type"] = "application/json"
            config_kwargs["response_schema"] = Digest

        gen_config = types.GenerateContentConfig(**config_kwargs)

        response = await asyncio.wait_for(
            self._client.aio.models.generate_content(
                model=self.model,
                contents=user_prompt,
                config=gen_config,
            ),
            timeout=self.timeout_seconds,
        )

        raw_text = (getattr(response, "text", None) or "").strip()
        if not raw_text:
            raise ValueError("Resposta vazia recebida do modelo Gemini.")

        if use_schema:
            digest = Digest.model_validate_json(raw_text)
        else:
            extracted = extract_json_from_text(raw_text)
            digest = Digest.model_validate_json(extracted)

        domains = _extract_grounding_domains(response)
        return digest, domains

    async def _run_step(
        self,
        *,
        step: str,
        system_prompt: str,
        user_prompt: str,
    ) -> tuple[Digest | None, str | None]:
        """Executes a single ladder step.

        Returns (digest, skip_reason) where skip_reason can be:
        - 'schema_combo': skip schema+search (go to A')
        - 'no_grounding': grounding unavailable (skip A/A', go to B)
        - 'failed': generic step failure
        """
        parse_failures = 0
        transient_attempts = 0

        while True:
            try:
                digest, domains = await self._call_model(
                    step=step,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                )
                self.last_grounding_domains = domains
                return digest, None

            except genai_errors.APIError as exc:
                code = getattr(exc, "code", None)
                message = str(exc)
                logger.warning("Gemini APIError no Modo %s (code=%s): %s", step, code, message)

                if code in (429, 503):
                    transient_attempts += 1
                    if transient_attempts < self.max_retries:
                        delay = 2 ** (transient_attempts - 1)
                        logger.warning(
                            "Retry %d/%d no Modo %s apos %ds (erro %s).",
                            transient_attempts,
                            self.max_retries,
                            step,
                            delay,
                            code,
                        )
                        await asyncio.sleep(delay)
                        continue
                    return None, "failed"

                if step == "A" and _is_schema_grounding_incompatibility(code, message):
                    logger.warning("Schema + google_search nao suportados neste modelo; descendo para Modo A'.")
                    return None, "schema_combo"

                if step in ("A", "A'") and _is_grounding_unavailable(code, message):
                    logger.warning("Grounding com google_search indisponivel nesta chave/modelo; descendo para Modo B.")
                    return None, "no_grounding"

                return None, "failed"

            except (asyncio.TimeoutError, TimeoutError) as exc:
                transient_attempts += 1
                logger.warning("Timeout no Modo %s (%s).", step, exc)
                if transient_attempts < self.max_retries:
                    delay = 2 ** (transient_attempts - 1)
                    await asyncio.sleep(delay)
                    continue
                return None, "failed"

            except (ValidationError, ValueError) as exc:
                parse_failures += 1
                logger.warning(
                    "Falha de JSON/validacao Pydantic no Modo %s (tentativa %d/2): %s",
                    step,
                    parse_failures,
                    exc,
                )
                if parse_failures < 2:
                    continue
                return None, "failed"

            except Exception as exc:
                logger.warning("Erro inesperado no Modo %s: %s", step, exc)
                return None, "failed"

    async def curate(
        self,
        mode: str,
        n: int,
        already_sent: list[str],
        candidates: list[Article] | None,
        *,
        date_str: str = "",
        already_sent_days: int = 3,
        max_candidates: int = 40,
        timezone_name: str = "America/Fortaleza",
    ) -> tuple[Digest | None, str]:
        """Executes the AI curation ladder. Returns (digest, mode_used)."""
        self.last_grounding_domains = set()
        steps = self._allowed_steps(mode)
        if not steps:
            return None, "C"

        failed_steps: set[str] = set()
        grounding_unavailable = False

        for step in steps:
            if step in failed_steps:
                continue
            if grounding_unavailable and step in ("A", "A'"):
                continue

            if step == "A":
                system_prompt = SYSTEM_PROMPT_A
                user_prompt = build_user_prompt_a(
                    date_str=date_str,
                    n=n,
                    days=already_sent_days,
                    already_sent_titles=already_sent,
                    timezone_name=timezone_name,
                )
            elif step == "A'":
                system_prompt = SYSTEM_PROMPT_A_PRIME
                user_prompt = build_user_prompt_a(
                    date_str=date_str,
                    n=n,
                    days=already_sent_days,
                    already_sent_titles=already_sent,
                    timezone_name=timezone_name,
                )
            elif step == "B":
                if not candidates:
                    logger.warning("Modo B ignorado: nenhum candidato RSS/HN disponivel.")
                    failed_steps.add("B")
                    continue
                system_prompt = SYSTEM_PROMPT_B
                user_prompt = build_user_prompt_b(
                    date_str=date_str,
                    n=n,
                    days=already_sent_days,
                    already_sent_titles=already_sent,
                    candidates=candidates,
                    max_candidates=max_candidates,
                    timezone_name=timezone_name,
                )
            else:
                continue

            digest, skip_reason = await self._run_step(
                step=step,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )

            if digest is not None:
                logger.info(
                    "Curadoria Gemini concluida no Modo %s: %d itens retornados.",
                    step,
                    len(digest.items),
                )
                return digest, step

            failed_steps.add(step)
            if skip_reason == "no_grounding":
                grounding_unavailable = True

        return None, "C"
