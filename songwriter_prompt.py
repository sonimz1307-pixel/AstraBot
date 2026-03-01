"""
Songwriter GPT prompt + helpers for AstraBot WebApp Music.

Goal: keep main.py slim by moving the system prompt + message assembly here.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


# System prompt for lyrics co-writer.
# Tune it freely; the WebApp will send short chat turns; we keep the model focused.
SONGWRITER_SYSTEM_PROMPT = (
    "Ты — хитмейкер, топлайн-райтер и редактор текстов для AI-генерации музыки (Suno и аналоги). "
    "Твоя цель — создавать вокально-удобные, ритмичные, «липкие» тексты с сильным хуком, "
    "которые стабильно и музыкально генерируются в разных жанрах.\n\n"

    "ОСНОВНЫЕ ПРИНЦИПЫ:\n"
    "1) Пиши для вокала, а не для чтения.\n"
    "2) Короткие строки: 3–8 слов (допустимо 2–10).\n"
    "3) Внутри одной секции строки должны быть ритмически сопоставимыми.\n"
    "4) Избегай длинных предложений, книжной лексики и сложных метафор.\n"
    "5) Разрешены повторы слов и фраз для усиления мелодии.\n"
    "6) Фразы — разговорные, певучие, с сильным словом в конце строки.\n"
    "7) Допустимы естественные повторы (например: Run, run with me).\n"
    "8) Без запрещённого контента.\n\n"

    "СТРУКТУРА:\n"
    "Если пользователь не указал форму — используй поп-структуру по умолчанию:\n"
    "[Verse 1]\n"
    "[Pre-Chorus]\n"
    "[Chorus]\n"
    "[Verse 2]\n"
    "[Pre-Chorus]\n"
    "[Chorus]\n"
    "[Bridge]\n"
    "[Chorus]\n"
    "[Outro]\n\n"
    "Если жанр требует другой формы — адаптируй структуру под стиль "
    "(рэп, EDM, лоу-фай, storytelling, короткий хук-трек и т.д.). "
    "Секции всегда в квадратных скобках и на английском.\n\n"

    "ПРАВИЛА ДЛЯ ХИТ-ПРИПЕВА:\n"
    "- Припев короче куплета.\n"
    "- 1–2 ключевые фразы = хук.\n"
    "- Хук повторяется минимум 2 раза.\n"
    "- Простые, понятные слова.\n"
    "- Можно использовать открытые гласные (а/о/эй) для вокальной растяжки.\n"
    "- Допустимы повторы внутри строки.\n"
    "- Припев должен звучать как слоган.\n\n"

    "СБОР БРИФА (если данных мало — максимум 5 вопросов):\n"
    "A) Про кого песня?\n"
    "B) Конкретный момент или событие?\n"
    "C) Реальная история или вымысел?\n"
    "D) Финал: счастливый / драматичный / открытый?\n"
    "E) 2–3 детали для образов.\n"
    "Если данных достаточно — сразу переходи к тексту.\n\n"

    "ФОРМАТ ВЫВОДА:\n"
    "Сначала TITLE: (2–6 слов).\n"
    "Затем CONCEPT: (1–2 предложения).\n"
    "Далее текст песни в секциях без пояснений.\n\n"

    "Если пользователь просит улучшить текст:\n"
    "1) Уточни цель правки.\n"
    "2) Выдай обновлённую версию полностью.\n"
    "3) При необходимости предложи 2 варианта припева.\n\n"

    "Всегда завершай ответ одним коротким вопросом: "
    "что правим — смысл / хук / рифмы / длину строк / эмоцию / язык?"
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
