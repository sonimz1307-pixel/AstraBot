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


SITE_BUILDER_GLOBAL_RULES = """
Ты создаёшь профессиональный одностраничный сайт без фото в стиле premium SaaS / modern business landing.

ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА:
1. Сайт всегда должен быть адаптивным и корректно работать на desktop и mobile.
2. Мобильная версия входит в базовый результат по умолчанию.
3. Нельзя генерировать незавершённые interactive-фичи.
4. Нельзя генерировать мёртвый код.
5. Нельзя генерировать мусорный контент, placeholder-текст, TODO, lorem ipsum, 1111, test, demo filler.
6. Результат должен выглядеть как production-ready статический сайт.

ТРЕБОВАНИЯ К ВЕРСТКЕ:
1. Используй чистый HTML, CSS и JS без внешних библиотек.
2. Обязательно добавляй meta viewport.
3. Layout должен быть responsive для desktop, tablet и mobile.
4. Используй предсказуемую структуру: header, hero, sections, faq/contacts/cta, footer.
5. Контент не должен ломать сетку на узких экранах.
6. Кнопки, карточки, формы, меню и секции должны корректно перестраиваться на мобильных.
7. Текст не должен выходить за контейнеры.
8. Нельзя делать горизонтальный скролл на мобильных.

ТРЕБОВАНИЯ К MOBILE:
1. Mobile-first поведение обязательно учитывать.
2. На ширинах около 390px и 412px сайт должен оставаться читаемым и удобным.
3. Hero, карточки, CTA, контакты и FAQ должны нормально складываться в одну колонку.
4. Кнопки на мобильных должны быть удобны для нажатия.
5. Отступы, размеры текста и spacing должны быть адаптированы под телефон.
6. Навигация на мобильных должна быть полностью рабочей.

ПРАВИЛА ДЛЯ НАВИГАЦИИ:
1. Используй только один единый breakpoint для CSS и JS.
2. Один и тот же breakpoint должен применяться везде. Рекомендуемый breakpoint: 980px.
3. Если делаешь burger menu:
   - CSS и JS должны быть полностью согласованы
   - все используемые JS-классы обязаны иметь стили в CSS
   - состояния открытия/закрытия должны быть полностью описаны
4. Если mobile menu зависит от JS, должен быть graceful fallback:
   - базовая навигация не должна становиться недоступной при сбое JS
5. Нельзя создавать JS-логику для меню без полной CSS-реализации.
6. Нельзя добавлять классы типа nav-toggle, nav--open, menu-open без готовых CSS-правил.

ПРАВИЛА ДЛЯ JS:
1. JS только улучшает UX, а не является единственной точкой работоспособности сайта.
2. Не добавляй классы, для которых нет CSS-стилей.
3. Не добавляй hover / scroll / hidden / open состояния, если они не реализованы визуально.
4. Нельзя генерировать мёртвую логику.
5. JS должен быть минимальным, понятным и завершённым.
6. Не используй сложные эффекты ради эффекта.

ПРАВИЛА ДЛЯ FAQ:
1. Предпочтительный вариант — чистый нативный <details>/<summary>.
2. Не смешивай нативное поведение details с хрупким кастомным click-контролем.
3. Если нужен accordion, он должен быть реализован последовательно и без конфликтов.
4. Не делай полу-кастомный FAQ.

ПРАВИЛА КАЧЕСТВА:
1. Дизайн должен выглядеть дорого, чисто и современно.
2. Не перегружай интерфейс лишними тенями, странными градиентами и хаотичными эффектами.
3. Держи сильную типографику, хорошие отступы и визуальную иерархию.
4. Сайт должен быть готов к показу клиенту без ощущения сырого шаблона.
5. Без незавершённых фич, без недостающих стилей, без конфликтов CSS/JS.

КОНТЕНТНЫЕ ПРАВИЛА:
1. Если пользователь дал свои тексты — используй их приоритетно.
2. Можно улучшать подачу, но нельзя ломать смысл.
3. Не придумывай факты о бизнесе без явной необходимости.
4. Заголовки, CTA и блоки должны быть логичными и коммерчески сильными.

ПЕРЕД ФИНАЛИЗАЦИЕЙ СДЕЛАЙ ВНУТРЕННЮЮ ПРОВЕРКУ:
- есть ли mobile adaptation
- совпадает ли breakpoint в CSS и JS
- нет ли JS-классов без CSS
- нет ли CSS-состояний без использования
- нет ли мёртвого JS-кода
- нет ли placeholder / мусорного текста
- не ломается ли навигация без JS
- корректно ли работает FAQ
- нет ли горизонтального скролла на мобильных
- выглядит ли сайт как production-ready

Если какая-то интерактивная функция не доведена до конца, не генерируй её вообще.
Лучше простой, чистый и полностью рабочий сайт, чем красивый, но недоделанный.
""".strip()


SITE_BUILDER_HARD_BANS = """
Запрещено:
- генерировать незавершённое mobile menu
- генерировать разные breakpoint в CSS и JS
- генерировать JS-классы без CSS-правил
- генерировать CSS-состояния без использования
- генерировать мёртвый код
- генерировать placeholder-текст и мусор
- делать mobile version зависимой только от JS без fallback
""".strip()


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


def _language_hint_from_text(text: str) -> str:
    text = text or ""
    if re.search(r"[А-Яа-яЁё]", text):
        return "Все тексты сайта пиши на русском языке."
    return "All visible site copy should be in English."


async def _chat_completion(
    *,
    system_prompt: str,
    user_prompt: str,
    model: Optional[str] = None,
    max_tokens: int = 12000,
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

    async with httpx.AsyncClient(timeout=240.0) as client:
        response = await client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
    if response.status_code != 200:
        raise SiteBuilderLLMError(f"OpenAI error {response.status_code}: {response.text[:4000]}")

    data = response.json()
    message = (((data.get("choices") or [{}])[0]).get("message") or {})
    text = _extract_text_content(message.get("content"))
    if not text:
        print(f"[site][llm] empty text raw_response={json.dumps(data, ensure_ascii=False)[:8000]}", flush=True)
        raise SiteBuilderLLMError("Model returned empty text")
    return text


async def normalize_brief(*, brief_raw: str, extra_texts_raw: Optional[str], model: Optional[str] = None) -> Dict[str, Any]:
    language_hint = _language_hint_from_text((brief_raw or "") + "\n" + (extra_texts_raw or ""))
    system_prompt = (
        "Ты нормализуешь бриф на создание коммерческого одностраничного сайта. "
        "Нужно вернуть только JSON без пояснений. Не выдумывай факты, которых нет. "
        "Если поле не дано, оставь пустую строку или пустой массив. "
        f"{language_hint}"
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
    "professional_saas_quality": true,
    "desktop_and_mobile_required": true
  }},
  "extra_text_summary": ""
}}

БРИФ:
{brief_raw}

ДОПОЛНИТЕЛЬНЫЕ ТЕКСТЫ:
{extra_texts_raw or ""}
'''
    raw = await _chat_completion(system_prompt=system_prompt, user_prompt=user_prompt, model=model, max_tokens=5000)
    return _extract_json_object(raw)


async def build_blueprint(*, structured_context: Dict[str, Any], model: Optional[str] = None) -> Dict[str, Any]:
    system_prompt = (
        "Ты UX-стратег и копирайтер лендингов. Верни только JSON-план одностраничного сайта "
        "в стиле premium SaaS / modern business. Без фото. Сайт обязан быть desktop + mobile responsive."
    )
    user_prompt = f'''
{SITE_BUILDER_GLOBAL_RULES}

{SITE_BUILDER_HARD_BANS}

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
    "ui_notes": ["soft gradients", "card layout", "fully responsive", "consistent breakpoint 980px"]
  }},
  "implementation_notes": {{
    "header_strategy": "",
    "mobile_navigation_strategy": "",
    "breakpoint": "980px",
    "faq_strategy": "native_details"
  }}
}}

Требования к плану:
- сразу планируй рабочую mobile-версию
- не закладывай недоделанное mobile menu
- не планируй JS-фичи без явной необходимости
- навигация должна быть простой и надёжной
- FAQ предпочтительно через native details/summary
'''
    raw = await _chat_completion(system_prompt=system_prompt, user_prompt=user_prompt, model=model, max_tokens=7000)
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
        "Сохраняй общую структуру сайта, меняй только то, что логично по запросу. "
        "Не ломай адаптивность, mobile и working navigation."
    )
    user_prompt = f'''
{SITE_BUILDER_GLOBAL_RULES}

{SITE_BUILDER_HARD_BANS}

КОНТЕКСТ ПРОЕКТА:
{json.dumps(structured_context, ensure_ascii=False, indent=2)}

ТЕКУЩИЙ BLUEPRINT:
{json.dumps(current_blueprint or {{}}, ensure_ascii=False, indent=2)}

ТЕКУЩАЯ HTML ВЕРСИЯ:
{(current_version.get("html_content") or "")[:12000]}

ПРАВКИ КЛИЕНТА:
{revision_request}

Верни обновлённый blueprint в том же формате JSON, что и раньше.
Обязательно сохрани:
- desktop + mobile responsive
- единый breakpoint
- отсутствие мёртвого кода
- рабочую навигацию
'''
    raw = await _chat_completion(system_prompt=system_prompt, user_prompt=user_prompt, model=model, max_tokens=7000)
    return _extract_json_object(raw)


async def generate_html(*, structured_context: Dict[str, Any], blueprint: Dict[str, Any], model: Optional[str] = None) -> str:
    language_hint = _language_hint_from_text(json.dumps(structured_context, ensure_ascii=False))
    system_prompt = (
        "Ты senior frontend developer. Верни только содержимое файла index.html без markdown. "
        "Сайт статический, one-page, без внешних библиотек, без CDN. Подключай styles.css и script.js. "
        f"{language_hint}"
    )
    user_prompt = f'''
{SITE_BUILDER_GLOBAL_RULES}

{SITE_BUILDER_HARD_BANS}

Контекст:
{json.dumps(structured_context, ensure_ascii=False, indent=2)}

Blueprint:
{json.dumps(blueprint, ensure_ascii=False, indent=2)}

Сгенерируй только index.html.

Требования к HTML:
- чистая семантическая HTML5-разметка
- обязательны: doctype, lang, charset, viewport, title
- одна страница
- header, main, section, footer
- без фото
- CTA и контакты обязательно
- FAQ через нативные details/summary, если он нужен
- не добавляй мусорный текст, комментарии, TODO, markdown
- если делаешь mobile menu, его структура должна полностью соответствовать будущим CSS и JS
- не завязывай базовую навигацию только на JS
- классы должны быть аккуратными, предсказуемыми и немногочисленными
- готовность под desktop и mobile обязательна
'''
    return await _chat_completion(system_prompt=system_prompt, user_prompt=user_prompt, model=model, max_tokens=12000)


async def generate_css(*, structured_context: Dict[str, Any], blueprint: Dict[str, Any], html_content: str, model: Optional[str] = None) -> str:
    system_prompt = (
        "Ты senior frontend developer. Верни только содержимое файла styles.css без markdown. "
        "Сделай premium SaaS / modern business стиль, мобильную адаптацию и аккуратную типографику. "
        "CSS должен полностью покрывать все состояния, которые реально используются в HTML и JS."
    )
    user_prompt = f'''
{SITE_BUILDER_GLOBAL_RULES}

{SITE_BUILDER_HARD_BANS}

Контекст:
{json.dumps(structured_context, ensure_ascii=False, indent=2)}

Blueprint:
{json.dumps(blueprint, ensure_ascii=False, indent=2)}

HTML:
{html_content[:20000]}

Сгенерируй только styles.css.

Требования к CSS:
- без внешних CSS reset-библиотек
- mobile-first
- дизайн должен выглядеть дорого, чисто и современно
- используй один единый breakpoint, рекомендуемо 980px
- breakpoint должен быть явным и единым для всей логики навигации
- если в HTML/JS есть классы mobile menu, для них обязаны быть полные стили
- не добавляй стили для состояний, которых нет в HTML/JS
- исключи горизонтальный скролл на мобильных
- hero, cards, CTA, contacts, FAQ должны хорошо выглядеть на 390px и 412px
- не делай хаотичных теней, кислотных цветов и визуального мусора
'''
    return await _chat_completion(system_prompt=system_prompt, user_prompt=user_prompt, model=model, max_tokens=12000)


async def generate_js(*, blueprint: Dict[str, Any], html_content: str, model: Optional[str] = None) -> str:
    system_prompt = (
        "Ты senior frontend developer. Верни только содержимое файла script.js без markdown. "
        "Нужен только лёгкий и полностью завершённый vanilla JS. Если JS не нужен, верни минимальный безопасный файл."
    )
    user_prompt = f'''
{SITE_BUILDER_GLOBAL_RULES}

{SITE_BUILDER_HARD_BANS}

Blueprint:
{json.dumps(blueprint, ensure_ascii=False, indent=2)}

HTML:
{html_content[:20000]}

Сгенерируй только script.js.

Требования к JS:
- чистый vanilla JS
- без внешних библиотек
- JS должен быть минимальным
- JS не должен быть обязательным для доступа к базовой навигации
- не добавляй мёртвую логику
- не добавляй hover/scroll/hidden/open классы без полной CSS-реализации
- если делаешь mobile menu, используй тот же breakpoint, что и в CSS: 980px
- FAQ: либо не трогай native details вообще, либо реализуй поведение последовательно и безопасно
- не делай сложных эффектов ради эффекта
- код должен быть коротким, понятным и production-ready
'''
    return await _chat_completion(system_prompt=system_prompt, user_prompt=user_prompt, model=model, max_tokens=6000)


async def generate_readme(*, structured_context: Dict[str, Any], version_number: int, model: Optional[str] = None) -> str:
    language_hint = _language_hint_from_text(json.dumps(structured_context, ensure_ascii=False))
    system_prompt = (
        "Ты готовишь короткий README для клиента. Верни только plain text без markdown-блоков. "
        f"{language_hint}"
    )
    user_prompt = f'''
Контекст:
{json.dumps(structured_context, ensure_ascii=False, indent=2)}

Собери README.
Он должен объяснять:
- как открыть сайт локально
- какой файл главный
- как загрузить сайт на хостинг
- что это версия v{int(version_number)}
- что сайт адаптирован под desktop и mobile
'''
    return await _chat_completion(system_prompt=system_prompt, user_prompt=user_prompt, model=model, max_tokens=2500)
