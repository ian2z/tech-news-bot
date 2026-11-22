from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import discord

from bot.ai_schema import CATEGORY_ORDER, Category, ValidatedDigest, ValidatedNewsItem
from bot.models import clean_html

MAX_EMBEDS_PER_MESSAGE = 10
MAX_EMBED_TOTAL_CHARS = 6000
MAX_DESCRIPTION_CHARS = 4096

_MENTION_RE = re.compile(r"@(everyone|here)|<@[!&]?\d+>")

# Identidade visual definida em guia_de_implementa_o_bot_de_not_cias_tech.md
CATEGORY_META: dict[Category, tuple[str, str, int]] = {
    "critico": ("🚨", "URGENTE / ZERO-DAY [CRÍTICO]", 0xF97316),
    "sec": ("🛡️", "CIBERSEGURANÇA & VULNERABILIDADES [SEC]", 0xEF4444),
    "ai": ("🧠", "INTELIGÊNCIA ARTIFICIAL & LLMS [AI / LLM]", 0x8B5CF6),
    "dev": ("💻", "CODIFICAÇÃO, ARQUITETURA & DEVOPS [DEV]", 0x3B82F6),
    "tech": ("🌐", "TECH GERAL, HARDWARE & MERCADO [TECH]", 0x10B981),
}

CONFIDENCE_BADGE: dict[str, str] = {
    "alta": "✅ Verificada",
    "média": "🟡 Fonte única",
    "baixa": "⚠️ Não confirmada",
}


def sanitize_text(text: str | None) -> str:
    cleaned = clean_html(text) or ""
    return _MENTION_RE.sub(lambda m: m.group(0).replace("@", "@\u200b"), cleaned)


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3].rstrip() + "..."


def embed_char_count(embed: discord.Embed) -> int:
    total = 0
    if embed.title:
        total += len(embed.title)
    if embed.description:
        total += len(embed.description)
    if embed.footer and embed.footer.text:
        total += len(embed.footer.text)
    if embed.author and embed.author.name:
        total += len(embed.author.name)
    for f in embed.fields:
        total += len(f.name) + len(f.value)
    return total


def _format_item_bullet(item: ValidatedNewsItem) -> str:
    flag = "🇧🇷" if item.lang == "PT-BR" else "🇺🇸"
    title = sanitize_text(item.title)
    summary = sanitize_text(item.summary)
    source = sanitize_text(item.source_name) or "Fonte"

    meta_parts = [f"Fonte: {source}"]
    if item.origin != "feed":
        badge = CONFIDENCE_BADGE.get(item.confidence)
        if badge:
            meta_parts.append(badge)

    lines = [f"• {flag} **[{title}]({item.url})**"]
    if summary:
        lines.append(f"  {summary}")
    lines.append(f"  └ *{' · '.join(meta_parts)}*")
    return "\n".join(lines)


def build_category_embeds(
    category: Category,
    items: list[ValidatedNewsItem],
    *,
    updated_at: datetime,
) -> list[discord.Embed]:
    if not items:
        return []

    emoji, label, color = CATEGORY_META.get(category, ("🌐", "TECH GERAL [TECH]", 0x10B981))
    time_label = updated_at.strftime("%H:%M")

    # Split into multiple embeds of the same category if description exceeds 4096 chars
    chunks: list[list[str]] = []
    current_chunk: list[str] = []
    current_len = 0

    for item in items:
        bullet = _format_item_bullet(item)
        added_len = len(bullet) + (2 if current_chunk else 0)
        if current_chunk and (current_len + added_len > MAX_DESCRIPTION_CHARS):
            chunks.append(current_chunk)
            current_chunk = [bullet]
            current_len = len(bullet)
        else:
            current_chunk.append(bullet)
            current_len += added_len

    if current_chunk:
        chunks.append(current_chunk)

    embeds: list[discord.Embed] = []
    total_items = len(items)
    for idx, chunk in enumerate(chunks):
        desc = truncate("\n\n".join(chunk), MAX_DESCRIPTION_CHARS)
        embed = discord.Embed(
            description=desc,
            color=color,
            timestamp=updated_at,
        )
        suffix = f" ({idx + 1}/{len(chunks)})" if len(chunks) > 1 else ""
        embed.set_author(name=f"{emoji} {label}{suffix}")
        destaque_label = "destaque" if total_items == 1 else "destaques"
        embed.set_footer(text=f"Resumo Tech 24h • {total_items} {destaque_label} • Atualizado às {time_label}")
        embeds.append(embed)

    return embeds


def format_digest_messages(
    digest: ValidatedDigest,
    *,
    tz: ZoneInfo,
    now: datetime | None = None,
) -> list[tuple[str | None, list[discord.Embed]]]:
    if not digest.items:
        return []

    current_dt = (now or datetime.now(tz)).astimezone(tz)
    date_label = current_dt.strftime("%d/%m/%Y")
    clean_intro = sanitize_text(digest.intro)
    header = f"☀️ **Bom dia! Aqui está o seu Radar Tech de {date_label}:**"
    if clean_intro:
        header = f"{header}\n{clean_intro}"

    grouped: dict[Category, list[ValidatedNewsItem]] = defaultdict(list)
    for item in sorted(digest.items, key=lambda x: x.rank):
        grouped[item.category].append(item)

    embeds: list[discord.Embed] = []
    for category in CATEGORY_ORDER:
        cat_items = grouped.get(category)
        if cat_items:
            embeds.extend(build_category_embeds(category, cat_items, updated_at=current_dt))

    messages: list[tuple[str | None, list[discord.Embed]]] = []
    batch: list[discord.Embed] = []
    batch_chars = 0

    for embed in embeds:
        chars = embed_char_count(embed)
        would_exceed_count = len(batch) >= MAX_EMBEDS_PER_MESSAGE
        would_exceed_chars = batch and (batch_chars + chars > MAX_EMBED_TOTAL_CHARS)

        if would_exceed_count or would_exceed_chars:
            content = header if not messages else None
            messages.append((content, batch))
            batch = []
            batch_chars = 0

        batch.append(embed)
        batch_chars += chars

    if batch:
        content = header if not messages else None
        messages.append((content, batch))

    return messages
