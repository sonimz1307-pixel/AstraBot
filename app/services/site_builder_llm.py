from __future__ import annotations

import json
import os
from typing import Any

import requests

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("SITE_BUILDER_MODEL", "gpt-5.4")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")


def _headers() -> dict[str, str]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")
    return {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }


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


def _extract_response_text(data: dict[str, Any]) -> str:
    # Responses API style
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    output = data.get("output")
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            text = _extract_text_content(content)
            if text:
                parts.append(text)
        if parts:
            return "\n".join(parts).strip()

    # Chat Completions style
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        choice0 = choices[0] if isinstance(choices[0], dict) else {}
        message = choice0.get("message") if isinstance(choice0, dict) else {}
        if isinstance(message, dict):
            content = message.get("content")
            text = _extract_text_content(content)
            if text:
                return text
        text = choice0.get("text") if isinstance(choice0, dict) else None
        if isinstance(text, str) and text.strip():
            return text.strip()

    return ""


def _post_json(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{OPENAI_BASE_URL}{path}"
    resp = requests.post(url, headers=_headers(), json=payload, timeout=300)
    try:
        data = resp.json()
    except Exception:
        raise RuntimeError(f"OpenAI non-JSON response: status={resp.status_code} body={resp.text[:1000]}")
    if resp.status_code >= 400:
        raise RuntimeError(
            f"OpenAI HTTP {resp.status_code}: {json.dumps(data, ensure_ascii=False)[:1500]}"
        )
    return data


def _call_model_json(system_prompt: str, user_prompt: str) -> str:
    payload = {
        "model": OPENAI_MODEL,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
            {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
        ],
    }
    data = _post_json("/responses", payload)
    text = _extract_response_text(data)
    if not text:
        debug_preview = json.dumps(data, ensure_ascii=False)[:3000]
        print(f"[site][llm] empty text raw_response={debug_preview}", flush=True)
        raise RuntimeError("Model returned empty text")
    return text.strip()


def normalize_brief(brief_raw: str, extra_texts_raw: str | None = None) -> dict[str, Any]:
    system_prompt = (
        "Ты извлекаешь из брифа данные для генерации одностраничного сайта без фото. "
        "Верни только JSON-объект без markdown."
    )
    user_prompt = (
        f"Бриф:\n{brief_raw}\n\n"
        f"Дополнительные тексты:\n{extra_texts_raw or ''}\n\n"
        "Верни JSON с полями: project_name, niche, audience, goal, offer, "
        "sections_requested, style_preferences, cta, contacts, extra_texts_raw."
    )
    text = _call_model_json(system_prompt, user_prompt)
    try:
        return json.loads(text)
    except Exception:
        print(f"[site][llm] normalize_brief non_json={text[:2000]}", flush=True)
        raise RuntimeError("normalize_brief returned non-JSON")


def build_blueprint(structured: dict[str, Any]) -> dict[str, Any]:
    system_prompt = (
        "Ты проектируешь premium SaaS / modern business landing page без фото. "
        "Верни только JSON-объект без markdown."
    )
    user_prompt = (
        "На основе данных проекта верни blueprint сайта в JSON. "
        "Нужны поля: title, sections, tone, visual_direction, cta, notes.\n\n"
        f"DATA:\n{json.dumps(structured, ensure_ascii=False)}"
    )
    text = _call_model_json(system_prompt, user_prompt)
    try:
        return json.loads(text)
    except Exception:
        print(f"[site][llm] build_blueprint non_json={text[:2000]}", flush=True)
        raise RuntimeError("build_blueprint returned non-JSON")


def generate_index_html(structured: dict[str, Any], blueprint: dict[str, Any]) -> str:
    system_prompt = (
        "Сгенерируй чистый production-ready index.html для адаптивного одностраничного сайта. "
        "Не добавляй markdown fences."
    )
    user_prompt = (
        f"STRUCTURED:\n{json.dumps(structured, ensure_ascii=False)}\n\n"
        f"BLUEPRINT:\n{json.dumps(blueprint, ensure_ascii=False)}"
    )
    return _call_model_json(system_prompt, user_prompt)


def generate_styles_css(structured: dict[str, Any], blueprint: dict[str, Any]) -> str:
    system_prompt = (
        "Сгенерируй чистый styles.css для адаптивного premium SaaS сайта. "
        "Не добавляй markdown fences."
    )
    user_prompt = (
        f"STRUCTURED:\n{json.dumps(structured, ensure_ascii=False)}\n\n"
        f"BLUEPRINT:\n{json.dumps(blueprint, ensure_ascii=False)}"
    )
    return _call_model_json(system_prompt, user_prompt)


def generate_script_js(structured: dict[str, Any], blueprint: dict[str, Any]) -> str:
    system_prompt = (
        "Сгенерируй минимальный script.js для сайта. Только нужный код, без markdown fences."
    )
    user_prompt = (
        f"STRUCTURED:\n{json.dumps(structured, ensure_ascii=False)}\n\n"
        f"BLUEPRINT:\n{json.dumps(blueprint, ensure_ascii=False)}"
    )
    return _call_model_json(system_prompt, user_prompt)


def generate_readme(structured: dict[str, Any]) -> str:
    project_name = (
        structured.get("project_name")
        or structured.get("title")
        or "site"
    )
    return (
        f"{project_name}\n\n"
        "Файлы сайта:\n"
        "- index.html\n"
        "- styles.css\n"
        "- script.js\n\n"
        "Как открыть:\n"
        "1. Распакуйте архив.\n"
        "2. Откройте index.html в браузере.\n\n"
        "Как опубликовать:\n"
        "Загрузите файлы на любой статический хостинг.\n"
    )
