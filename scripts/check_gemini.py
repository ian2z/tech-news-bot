from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

# Allow running directly as `python scripts/check_gemini.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.ai_schema import Digest


def main() -> int:
    load_dotenv()
    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    model = (os.getenv("GEMINI_MODEL") or "gemini-3.8-flash").strip()

    if not api_key:
        print("ERRO: GEMINI_API_KEY nao encontrada no .env", flush=True)
        return 1

    print(f"Modelo configurado: {model}", flush=True)
    print("-" * 60, flush=True)

    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=30_000),
    )
    search_tool = types.Tool(google_search=types.GoogleSearch())

    results = {"test1": False, "test2": False, "test3": False}

    # Teste 1: Chamada simples sem ferramentas
    print("[Teste 1] Chamada simples (sem ferramentas)...", flush=True)
    try:
        r1 = client.models.generate_content(
            model=model,
            contents="Responda apenas com a palavra: OK",
            config=types.GenerateContentConfig(max_output_tokens=32),
        )
        print(f"  STATUS: OK | resposta={((r1.text or '').strip()[:80])!r}", flush=True)
        results["test1"] = True
    except Exception as exc:
        print(f"  STATUS: FALHOU | {type(exc).__name__}: {exc}", flush=True)

    print("-" * 60, flush=True)

    # Teste 2: Chamada com google_search
    print("[Teste 2] Chamada com google_search (grounding)...", flush=True)
    try:
        r2 = client.models.generate_content(
            model=model,
            contents="Quais as 2 principais noticias de tecnologia de hoje? Responda de forma breve em portugues.",
            config=types.GenerateContentConfig(tools=[search_tool]),
        )
        text2 = (r2.text or "").strip()
        meta2 = r2.candidates[0].grounding_metadata if r2.candidates else None
        chunks2 = getattr(meta2, "grounding_chunks", None) if meta2 else None
        print("  STATUS: OK", flush=True)
        print(f"  texto (trecho): {text2[:250]!r}", flush=True)
        print(f"  grounding_metadata presente: {meta2 is not None}", flush=True)
        print(f"  grounding_chunks count: {len(chunks2) if chunks2 else 0}", flush=True)
        results["test2"] = True
    except Exception as exc:
        print(f"  STATUS: FALHOU | {type(exc).__name__}: {exc}", flush=True)

    print("-" * 60, flush=True)

    # Teste 3: Chamada com google_search + response_schema
    print("[Teste 3] Chamada com google_search + response_schema (Digest)...", flush=True)
    try:
        r3 = client.models.generate_content(
            model=model,
            contents=(
                "Pesquise 2 noticias de tecnologia de hoje e responda no schema JSON Digest em portugues do Brasil."
            ),
            config=types.GenerateContentConfig(
                tools=[search_tool],
                response_mime_type="application/json",
                response_schema=Digest,
                temperature=1.0,
            ),
        )
        text3 = (r3.text or "").strip()
        digest = Digest.model_validate_json(text3)
        meta3 = r3.candidates[0].grounding_metadata if r3.candidates else None
        chunks3 = getattr(meta3, "grounding_chunks", None) if meta3 else None
        print(f"  STATUS: OK | itens parseados={len(digest.items)}", flush=True)
        print(
            f"  grounding_chunks: {'preenchido (' + str(len(chunks3)) + ')' if chunks3 else 'vazio'}",
            flush=True,
        )
        results["test3"] = True
    except Exception as exc:
        print(f"  STATUS: FALHOU | {type(exc).__name__}: {exc}", flush=True)

    print("-" * 60, flush=True)
    if results["test3"]:
        print("CONCLUSAO: Modo A suportado (google_search + response_schema).", flush=True)
    elif results["test2"]:
        print("CONCLUSAO: Modo A' suportado (google_search sem response_schema).", flush=True)
    elif results["test1"]:
        print("CONCLUSAO: Grounding indisponivel; usar Modo B (curadoria de candidatos RSS/HN).", flush=True)
    else:
        print("CONCLUSAO: Chave ou modelo indisponivel; bot operara no Modo C (RSS puro).", flush=True)

    return 0 if results["test1"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
