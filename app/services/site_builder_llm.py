from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional

import httpx

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
SITE_BUILDER_MODEL = (os.getenv("SITE_BUILDER_MODEL", "gpt-5.4") or "gpt-5.4").strip()


class SiteBuilderLLMError(RuntimeError):
    pass


_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL | re.IGNORECASE)


def _supports_temperature(model: str) -> bool:
    name = str(model or "").strip().lower()
    if not name:
        return True
    blocked_prefixes = ("gpt-5", "o1", "o3", "o4")
    return not any(name.startswith(prefix) for prefix in blocked_prefixes)


def _extract_text_content(message_content: Any) -> str:
    def _pick_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            for key in ("text", "output_text", "value"):
                v = value.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            if "content" in value:
                return _pick_text(value.get("content"))
            return ""
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                text = _pick_text(item)
                if text:
                    parts.append(text)
            return "\n".join(parts).strip()
        return str(value).strip()

    return _pick_text(message_content)


def _extract_json_object(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise SiteBuilderLLMError("Model returned empty JSON payload")
    match = _JSON_BLOCK_RE.search(raw)
    if match:
        raw = match.group(1).strip()
    else:
        first = raw.find("{")
        last = raw.rfind("}")
        if first >= 0 and last > first:
            raw = raw[first:last + 1]
    try:
        data = json.loads(raw)
    except Exception as exc:
        raise SiteBuilderLLMError(f"Failed to parse JSON: {exc}; raw={raw[:1200]}")
    if not isinstance(data, dict):
        raise SiteBuilderLLMError("Expected JSON object from model")
    return data


async def _chat_completion(
    *,
    system_prompt: str,
    user_prompt: str,
    model: Optional[str] = None,
    max_tokens: int = 4000,
    temperature: float = 0.2,
) -> str:
    if not OPENAI_API_KEY:
        raise SiteBuilderLLMError("OPENAI_API_KEY is not set")

    resolved_model = (model or SITE_BUILDER_MODEL).strip() or SITE_BUILDER_MODEL
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload: Dict[str, Any] = {
        "model": resolved_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_completion_tokens": int(max_tokens),
    }
    if _supports_temperature(resolved_model):
        payload["temperature"] = float(temperature)

    async with httpx.AsyncClient(timeout=180.0) as client:
        response = await client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
    if response.status_code != 200:
        raise SiteBuilderLLMError(f"OpenAI error {response.status_code}: {response.text[:2000]}")
    data = response.json()
    message = (((data.get("choices") or [{}])[0]).get("message") or {})
    text = _extract_text_content(message.get("content"))
    if not text:
        raise SiteBuilderLLMError("Model returned empty text")
    return text


async def normalize_brief(*, brief_raw: str, extra_texts_raw: Optional[str], model: Optional[str] = None) -> Dict[str, Any]:
    system_prompt = (
        "Ты нормализуешь бриф на создание коммерческого одностраничного сайта. "
        "Нужно вернуть только JSON без пояснений. Не выдумывай факты, которых нет. "
        "Если поле не дано, оставь пустую строку или пустой массив."
    )
    user_prompt = f'''
Собери JSON такого вида:
{{
  "project_name": "",
  "niche": "",
  "audience": "",
  "goal": "",
  "offer": "",
  "sections_requested": ["hero", "advantages", "services", "faq", "contacts"],
  "style_preferences": {{
    "visual_style": "",
    "colors": "",
    "tone": ""
  }},
  "cta": "",
  "contacts": {{
    "phone": "",
    "telegram": "",
    "whatsapp": "",
    "email": ""
  }},
  "key_points": [],
  "constraints": {{
    "no_photos": true,
    "one_page_only": true,
    "professional_saas_quality": true
  }},
  "extra_text_summary": ""
}}

БРИФ:
{brief_raw}

ДОПОЛНИТЕЛЬНЫЕ ТЕКСТЫ:
{extra_texts_raw or ""}
'''
    raw = await _chat_completion(system_prompt=system_prompt, user_prompt=user_prompt, model=model, max_tokens=2500)
    return _extract_json_object(raw)


async def build_blueprint(*, structured_context: Dict[str, Any], model: Optional[str] = None) -> Dict[str, Any]:
    system_prompt = (
        "Ты UX-стратег и копирайтер лендингов. Верни только JSON-план одностраничного сайта "
        "в стиле premium SaaS / modern business. Без фото."
    )
    user_prompt = f'''
Контекст проекта:
{json.dumps(structured_context, ensure_ascii=False, indent=2)}

Верни JSON вида:
{{
  "site_title": "",
  "hero": {{
    "eyebrow": "",
    "headline": "",
    "subheadline": "",
    "primary_cta": "",
    "secondary_cta": ""
  }},
  "sections": [
    {{
      "id": "hero",
      "title": "",
      "subtitle": "",
      "items": []
    }}
  ],
  "faq": [{{"q": "", "a": ""}}],
  "footer_note": "",
  "design_direction": {{
    "style_words": ["clean", "premium", "modern"],
    "palette_hint": "",
    "ui_notes": ["soft gradients", "card layout"]
  }}
}}
'''
    raw = await _chat_completion(system_prompt=system_prompt, user_prompt=user_prompt, model=model, max_tokens=3000)
    return _extract_json_object(raw)


async def apply_revision(
    *,
    structured_context: Dict[str, Any],
    current_blueprint: Dict[str, Any],
    current_version: Dict[str, Any],
    revision_request: str,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    system_prompt = (
        "Ты обновляешь план сайта по правкам клиента. Верни только JSON. "
        "Сохраняй общую структуру сайта, меняй только то, что логично по запросу."
    )
    user_prompt = f'''
КОНТЕКСТ ПРОЕКТА:
{json.dumps(structured_context, ensure_ascii=False, indent=2)}

ТЕКУЩИЙ BLUEPRINT:
{json.dumps(current_blueprint or {{}}, ensure_ascii=False, indent=2)}

ТЕКУЩАЯ HTML ВЕРСИЯ:
{(current_version.get("html_content") or "")[:10000]}

ПРАВКИ КЛИЕНТА:
{revision_request}

Верни обновлённый blueprint в том же формате JSON, что и раньше.
'''
    raw = await _chat_completion(system_prompt=system_prompt, user_prompt=user_prompt, model=model, max_tokens=3200)
    return _extract_json_object(raw)


async def generate_html(*, structured_context: Dict[str, Any], blueprint: Dict[str, Any], model: Optional[str] = None) -> str:
    system_prompt = (
        "Ты senior frontend developer. Верни только содержимое файла index.html без markdown. "
        "Сайт статический, one-page, без внешних библиотек, без CDN. Подключай styles.css и script.js. "
        "Тексты на русском, если вход на русском."
    )
    user_prompt = f'''
Контекст:
{json.dumps(structured_context, ensure_ascii=False, indent=2)}

Blueprint:
{json.dumps(blueprint, ensure_ascii=False, indent=2)}

Требования:
- чистая семантическая HTML5-разметка
- блок hero, секции, FAQ, footer
- без фото
- аккуратные классы
- адаптивная структура
- одна страница
- CTA и контакты обязательно
'''
    return await _chat_completion(system_prompt=system_prompt, user_prompt=user_prompt, model=model, max_tokens=5000)


async def generate_css(*, structured_context: Dict[str, Any], blueprint: Dict[str, Any], html_content: str, model: Optional[str] = None) -> str:
    system_prompt = (
        "Ты senior frontend developer. Верни только содержимое файла styles.css без markdown. "
        "Сделай premium SaaS / modern business стиль, мобильную адаптацию и аккуратную типографику."
    )
    user_prompt = f'''
Контекст:
{json.dumps(structured_context, ensure_ascii=False, indent=2)}

Blueprint:
{json.dumps(blueprint, ensure_ascii=False, indent=2)}

HTML:
{html_content[:12000]}

Требования:
- без CSS reset-библиотек
- хорошие отступы
- hero, cards, faq, footer
- mobile-first
- максимум качества без визуального мусора
'''
    return await _chat_completion(system_prompt=system_prompt, user_prompt=user_prompt, model=model, max_tokens=4500)


async def generate_js(*, blueprint: Dict[str, Any], html_content: str, model: Optional[str] = None) -> str:
    system_prompt = (
        "Ты senior frontend developer. Верни только содержимое файла script.js без markdown. "
        "Нужен только лёгкий vanilla JS: mobile menu, плавный скролл, FAQ accordion, micro interactions."
    )
    user_prompt = f'''
Blueprint:
{json.dumps(blueprint, ensure_ascii=False, indent=2)}

HTML:
{html_content[:12000]}

Требования:
- чистый vanilla JS
- не использовать внешние библиотеки
- без сложных анимаций
- код должен быть безопасным и коротким
'''
    return await _chat_completion(system_prompt=system_prompt, user_prompt=user_prompt, model=model, max_tokens=2200)


async def generate_readme(*, structured_context: Dict[str, Any], version_number: int, model: Optional[str] = None) -> str:
    system_prompt = "Ты готовишь короткий README для клиента. Верни только plain text без markdown-блоков."
    user_prompt = f'''
Контекст:
{json.dumps(structured_context, ensure_ascii=False, indent=2)}

Собери README на русском языке.
Он должен объяснять:
- как открыть сайт локально
- какой файл главный
- как загрузить сайт на хостинг
- что это версия v{int(version_number)}
'''
    return await _chat_completion(system_prompt=system_prompt, user_prompt=user_prompt, model=model, max_tokens=1200)
