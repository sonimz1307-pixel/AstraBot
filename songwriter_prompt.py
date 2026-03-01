"""
Songwriter GPT prompt + helpers for AstraBot WebApp Music.

Goal: keep main.py slim by moving the system prompt + message assembly here.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


# System prompt for lyrics co-writer.
# Tune it freely; the WebApp will send short chat turns; we keep the model focused.
SONGWRITER_SYSTEM_PROMPT = (
    "Ты — опытный сонграйтер и редактор текстов песен. "
    "Твоя задача: помогать пользователю написать текст песни для генерации музыки.\n\n"
    "Правила:\n"
    "1) Всегда уточняй недостающие вводные (жанр/настроение/язык/кому/о чём/стиль/референсы/цензура).\n"
    "2) Предлагай структуру: [Verse], [Pre-Chorus], [Chorus], [Bridge], [Outro] — по ситуации.\n"
    "3) Делай текст удобным для вставки в генератор: короткие строки, понятные секции.\n"
    "4) Если пользователь просит — делай варианты: 2–3 припева или 2 версии куплета.\n"
    "5) Всегда в конце спрашивай: что поправить (рифмы/смысл/длина/лексика/цензура/язык).\n"
    "6) Не выдумывай фактов о пользователе. Не используй запрещённый контент.\n"
)


def build_songwriter_messages(
    chat_history: Optional[List[Dict[str, Any]]],
    user_text: str,
    *,
    language: Optional[str] = None,
    genre: Optional[str] = None,
    mood: Optional[str] = None,
    references: Optional[str] = None,
) -> List[Dict[str, str]]:
    """
    Build OpenAI-style chat messages list:
      [{"role":"system","content":"..."}, {"role":"user","content":"..."}, ...]

    chat_history is expected as list of {"role": "user|assistant", "content": "..."} from WebApp.
    """
    sys = SONGWRITER_SYSTEM_PROMPT

    # Optional guiding context line (kept short to avoid token bloat)
    ctx_parts = []
    if language:
        ctx_parts.append(f"Язык: {language}")
    if genre:
        ctx_parts.append(f"Жанр: {genre}")
    if mood:
        ctx_parts.append(f"Настроение: {mood}")
    if references:
        ctx_parts.append(f"Референсы/вайб: {references}")

    if ctx_parts:
        sys = sys + "\n\nКонтекст:\n- " + "\n- ".join(ctx_parts)

    messages: List[Dict[str, str]] = [{"role": "system", "content": sys}]

    if chat_history:
        for m in chat_history:
            role = (m.get("role") or "").strip().lower()
            content = (m.get("content") or "").strip()
            if not content:
                continue
            if role not in ("user", "assistant"):
                # ignore unknown roles
                continue
            messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": user_text.strip()})
    return messages
