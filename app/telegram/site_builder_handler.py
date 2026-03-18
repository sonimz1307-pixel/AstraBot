from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.services.site_builder_billing import (
    SITE_CREATE_PRICE,
    SITE_REVISION_PRICE,
    charge_site_create,
    charge_site_revision,
    get_site_balance,
)
from app.services.site_builder_repo import (
    JOB_CREATE,
    JOB_REVISION,
    STATUS_PAYMENT_PENDING,
    STATUS_PREVIEW_READY,
    STATUS_WAITING_EXTRA_TEXTS,
    create_job,
    create_project,
    get_project_for_user,
    get_version_for_user,
    list_projects_for_user,
    update_project,
)
from app.services.site_builder_storage import download_zip
from queue_redis import enqueue_job

SITE_QUEUE_NAME = (os.getenv("SITE_QUEUE_NAME", "site") or "site").strip() or "site"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""

SITE_MENU_TEXT = (
    "🌐 Сайты\n\n"
    "Создание сайта — 30 токенов.\n"
    "В стоимость входит 1 бесплатный пакет правок.\n"
    "Все следующие правки — 10 токенов.\n\n"
    "Версия V1 делает одностраничный сайт без фото в стиле premium SaaS / modern business."
)

BRIEF_EXAMPLE = (
    "Название проекта: ...\n"
    "Чем занимается бизнес / продукт: ...\n"
    "Целевая аудитория: ...\n"
    "Главная цель сайта: ...\n"
    "Что нужно продвигать / продавать: ...\n"
    "Какие блоки нужны на сайте: ...\n"
    "Желаемый стиль / цвет / настроение: ...\n"
    "Контакты / CTA / способ связи: ..."
)

STATE_WAIT_BRIEF = "site_wait_brief"
STATE_WAIT_EXTRA_TEXTS = "site_wait_extra_texts"
STATE_WAIT_CREATE_CONFIRM = "site_wait_create_confirm"
STATE_WAIT_REVISION_TEXT = "site_wait_revision_text"
STATE_WAIT_PAID_REVISION_CONFIRM = "site_wait_paid_revision_confirm"


async def _tg_send_document_bytes(chat_id: int, data: bytes, *, filename: str, caption: Optional[str] = None) -> None:
    if not TG_API:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    files = {"document": (filename, data, "application/zip")}
    payload = {"chat_id": str(chat_id)}
    if caption:
        payload["caption"] = caption
    async with httpx.AsyncClient(timeout=120.0) as client:
        await client.post(f"{TG_API}/sendDocument", data=payload, files=files)


async def _send_message(deps: Dict[str, Any], chat_id: int, text: str, reply_markup: Optional[dict] = None) -> None:
    tg_send_message = deps.get("tg_send_message")
    if tg_send_message:
        await tg_send_message(chat_id, text, reply_markup=reply_markup)
        return
    if not TG_API:
        raise RuntimeError("tg_send_message dependency missing and TELEGRAM_BOT_TOKEN not set")
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    async with httpx.AsyncClient(timeout=30.0) as client:
        await client.post(f"{TG_API}/sendMessage", json=payload)


def site_menu_keyboard() -> dict:
    return {
        "keyboard": [
            [{"text": "🆕 Создать сайт"}],
            [{"text": "🗂 Мои сайты"}],
            [{"text": "⬅️ Назад"}],
        ],
        "resize_keyboard": True,
    }


def site_inline_preview_keyboard(project_id: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": f"🚀 Создать сайт за {SITE_CREATE_PRICE} токенов", "callback_data": f"site:create:confirm:{project_id}"}],
            [{"text": "✏️ Изменить бриф", "callback_data": f"site:brief:retry:{project_id}"}],
            [{"text": "🗂 Мои сайты", "callback_data": "site:projects"}],
        ]
    }


def site_project_actions_keyboard(project_id: str, *, current_version: int) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "✏️ Редактировать сайт", "callback_data": f"site:edit:start:{project_id}"}],
            [{"text": f"📦 Скачать v{int(current_version)}", "callback_data": f"site:download:{project_id}:{int(current_version)}"}],
            [{"text": "🗂 Мои сайты", "callback_data": "site:projects"}],
        ]
    }


def site_projects_keyboard(items: List[Dict[str, Any]]) -> dict:
    rows = []
    for item in items[:10]:
        project_id = str(item.get("id") or "")
        title = str(item.get("title") or "Сайт").strip() or "Сайт"
        version = int(item.get("current_version") or 0)
        rows.append([
            {"text": f"{title} · v{version}", "callback_data": f"site:project:{project_id}"}
        ])
    rows.append([{"text": "🆕 Создать сайт", "callback_data": "site:create"}])
    return {"inline_keyboard": rows}


_BRIEF_REQUIRED_KEYS = [
    "название проекта",
    "чем занимается",
    "целевая аудитория",
    "главная цель",
    "что нужно продвигать",
    "какие блоки",
    "желаемый стиль",
    "контакты",
]


def _normalize_brief_lines(text: str) -> Tuple[bool, List[str], str]:
    lines = [line.strip() for line in (text or "").replace("\r", "").split("\n") if line.strip()]
    if len(lines) < 8:
        return False, lines, "Бриф слишком короткий. Нужно минимум 8 заполненных строк."
    for line in lines:
        if ":" not in line:
            return False, lines, "Каждая строка должна быть в формате: Поле: значение"
        left, right = line.split(":", 1)
        if not left.strip() or not right.strip():
            return False, lines, "Каждая строка должна содержать название поля и заполненное значение."
    joined = "\n".join(lines).lower()
    found = sum(1 for key in _BRIEF_REQUIRED_KEYS if key in joined)
    if found < 4:
        return False, lines, "Бриф выглядит слишком размыто. Используй шаблон и названия полей из примера."
    return True, lines, ""


def _project_summary(project: Dict[str, Any]) -> str:
    brief = str(project.get("brief_raw") or "")
    lines = [line.strip() for line in brief.splitlines() if line.strip()]
    short = lines[:6]
    summary = "\n".join(f"• {line}" for line in short)
    extra = "есть" if str(project.get("extra_texts_raw") or "").strip() else "нет"
    return (
        "Проверьте ваш заказ:\n\n"
        f"{summary}\n\n"
        f"Доп. тексты: {extra}\n"
        f"Формат: одностраничный сайт без фото\n"
        f"Цена: {SITE_CREATE_PRICE} токенов"
    )


async def show_site_menu(*, chat_id: int, deps: Dict[str, Any]) -> None:
    await _send_message(deps, chat_id, SITE_MENU_TEXT, reply_markup=site_menu_keyboard())


async def show_projects(*, chat_id: int, user_id: int, deps: Dict[str, Any]) -> None:
    items = list_projects_for_user(int(user_id), limit=10)
    if not items:
        await _send_message(
            deps,
            chat_id,
            "У вас пока нет созданных сайтов. Нажмите «🆕 Создать сайт», чтобы начать.",
            reply_markup=site_menu_keyboard(),
        )
        return
    lines = ["🗂 Мои сайты:\n"]
    for item in items[:10]:
        lines.append(
            f"• {str(item.get('title') or 'Сайт')} — статус: {str(item.get('status') or '-')} — v{int(item.get('current_version') or 0)}"
        )
    await _send_message(deps, chat_id, "\n".join(lines), reply_markup=site_projects_keyboard(items))


async def start_site_brief_flow(*, chat_id: int, user_id: int, deps: Dict[str, Any]) -> None:
    deps["sb_set_user_state"](int(user_id), STATE_WAIT_BRIEF, {})
    await _send_message(
        deps,
        chat_id,
        "Отправьте бриф одним сообщением. Минимум 8 строк в формате «Поле: значение».\n\nПример:\n" + BRIEF_EXAMPLE,
        reply_markup=site_menu_keyboard(),
    )


async def handle_site_text(*, chat_id: int, user_id: int, incoming_text: str, deps: Dict[str, Any]) -> bool:
    state, payload = deps["sb_get_user_state"](int(user_id))
    text = (incoming_text or "").strip()
    if text == "🌐 Сайты":
        await show_site_menu(chat_id=chat_id, deps=deps)
        return True
    if text == "🆕 Создать сайт":
        await start_site_brief_flow(chat_id=chat_id, user_id=user_id, deps=deps)
        return True
    if text == "🗂 Мои сайты":
        await show_projects(chat_id=chat_id, user_id=user_id, deps=deps)
        return True

    if state == STATE_WAIT_BRIEF:
        ok, _lines, error = _normalize_brief_lines(text)
        if not ok:
            await _send_message(deps, chat_id, error + "\n\nПример:\n" + BRIEF_EXAMPLE, reply_markup=site_menu_keyboard())
            return True
        title = "Новый сайт"
        for line in text.splitlines():
            if line.lower().startswith("название проекта") and ":" in line:
                title = line.split(":", 1)[1].strip() or title
                break
        project = create_project(telegram_user_id=int(user_id), title=title, brief_raw=text)
        update_project(project["id"], {"status": STATUS_WAITING_EXTRA_TEXTS})
        deps["sb_set_user_state"](int(user_id), STATE_WAIT_EXTRA_TEXTS, {"project_id": project["id"]})
        kb = {
            "inline_keyboard": [
                [{"text": "📄 Да, отправить тексты", "callback_data": f"site:extra:yes:{project['id']}"}],
                [{"text": "⏭ Пропустить", "callback_data": f"site:extra:skip:{project['id']}"}],
            ]
        }
        await _send_message(
            deps,
            chat_id,
            "Есть ли у вас готовые тексты для сайта?\nМожно прислать описание компании, услуг, FAQ, цены, оффер или любые формулировки, которые обязательно нужно использовать.",
            reply_markup=kb,
        )
        return True

    if state == STATE_WAIT_EXTRA_TEXTS:
        project_id = str((payload or {}).get("project_id") or "")
        project = get_project_for_user(int(user_id), project_id)
        if not project:
            deps["sb_clear_user_state"](int(user_id))
            await _send_message(deps, chat_id, "Проект не найден. Начните заново.", reply_markup=site_menu_keyboard())
            return True
        update_project(project_id, {"extra_texts_raw": text, "status": STATUS_PREVIEW_READY})
        deps["sb_set_user_state"](int(user_id), STATE_WAIT_CREATE_CONFIRM, {"project_id": project_id})
        project = get_project_for_user(int(user_id), project_id) or project
        await _send_message(deps, chat_id, _project_summary(project), reply_markup=site_inline_preview_keyboard(project_id))
        return True

    if state == STATE_WAIT_REVISION_TEXT:
        project_id = str((payload or {}).get("project_id") or "")
        project = get_project_for_user(int(user_id), project_id)
        if not project:
            deps["sb_clear_user_state"](int(user_id))
            await _send_message(deps, chat_id, "Проект не найден.", reply_markup=site_menu_keyboard())
            return True
        revision_text = text.strip()
        if not revision_text:
            await _send_message(deps, chat_id, "Опишите правки одним сообщением.")
            return True
        if not bool(project.get("free_revision_used")):
            job = create_job(
                project_id=project_id,
                telegram_user_id=int(user_id),
                job_type=JOB_REVISION,
                tokens_cost=0,
                is_free_revision=True,
                request_raw=revision_text,
            )
            await enqueue_job({"job_id": job["id"], "kind": "site_revision"}, queue_name=SITE_QUEUE_NAME)
            deps["sb_clear_user_state"](int(user_id))
            await _send_message(
                deps,
                chat_id,
                "✅ Бесплатный пакет правок запущен. Как только новая версия будет готова, я пришлю ZIP-архив.",
                reply_markup=site_menu_keyboard(),
            )
            return True

        balance = get_site_balance(int(user_id))
        if balance < SITE_REVISION_PRICE:
            await _send_message(
                deps,
                chat_id,
                f"❌ Недостаточно токенов для правки. Нужно {SITE_REVISION_PRICE}, баланс: {balance}.",
                reply_markup=site_menu_keyboard(),
            )
            deps["sb_clear_user_state"](int(user_id))
            return True

        deps["sb_set_user_state"](
            int(user_id),
            STATE_WAIT_PAID_REVISION_CONFIRM,
            {"project_id": project_id, "revision_request": revision_text},
        )
        kb = {
            "inline_keyboard": [
                [{"text": f"🚀 Подтвердить правку за {SITE_REVISION_PRICE} токенов", "callback_data": f"site:edit:confirm:{project_id}"}],
                [{"text": "❌ Отмена", "callback_data": f"site:project:{project_id}"}],
            ]
        }
        await _send_message(
            deps,
            chat_id,
            f"Бесплатный пакет правок уже использован. Следующая правка будет стоить {SITE_REVISION_PRICE} токенов.\n\nПодтвердить запуск?",
            reply_markup=kb,
        )
        return True

    return False


async def handle_site_callback(*, chat_id: int, user_id: int, callback_data: str, deps: Dict[str, Any]) -> bool:
    data = str(callback_data or "")
    if not data.startswith("site:"):
        return False

    state_get = deps["sb_get_user_state"]
    state_set = deps["sb_set_user_state"]
    state_clear = deps["sb_clear_user_state"]

    if data == "site:create":
        await start_site_brief_flow(chat_id=chat_id, user_id=user_id, deps=deps)
        return True

    if data == "site:projects":
        await show_projects(chat_id=chat_id, user_id=user_id, deps=deps)
        return True

    if data.startswith("site:extra:yes:"):
        project_id = data.rsplit(":", 1)[-1]
        project = get_project_for_user(int(user_id), project_id)
        if not project:
            await _send_message(deps, chat_id, "Проект не найден.")
            return True
        state_set(int(user_id), STATE_WAIT_EXTRA_TEXTS, {"project_id": project_id})
        await _send_message(
            deps,
            chat_id,
            "Отправьте готовые тексты одним сообщением. Можно прислать описание компании, услуг, FAQ, цены, оффер и любые обязательные формулировки.",
            reply_markup=site_menu_keyboard(),
        )
        return True

    if data.startswith("site:extra:skip:"):
        project_id = data.rsplit(":", 1)[-1]
        project = get_project_for_user(int(user_id), project_id)
        if not project:
            await _send_message(deps, chat_id, "Проект не найден.")
            return True
        update_project(project_id, {"status": STATUS_PREVIEW_READY})
        state_set(int(user_id), STATE_WAIT_CREATE_CONFIRM, {"project_id": project_id})
        project = get_project_for_user(int(user_id), project_id) or project
        await _send_message(deps, chat_id, _project_summary(project), reply_markup=site_inline_preview_keyboard(project_id))
        return True

    if data.startswith("site:create:confirm:"):
        project_id = data.rsplit(":", 1)[-1]
        project = get_project_for_user(int(user_id), project_id)
        if not project:
            await _send_message(deps, chat_id, "Проект не найден.")
            return True
        balance = get_site_balance(int(user_id))
        if balance < SITE_CREATE_PRICE:
            await _send_message(
                deps,
                chat_id,
                f"❌ Недостаточно токенов. Нужно {SITE_CREATE_PRICE}, баланс: {balance}.",
                reply_markup=site_menu_keyboard(),
            )
            return True
        charge_site_create(user_id=int(user_id), project_id=project_id, meta={"title": project.get("title")})
        job = create_job(
            project_id=project_id,
            telegram_user_id=int(user_id),
            job_type=JOB_CREATE,
            tokens_cost=SITE_CREATE_PRICE,
            is_free_revision=False,
            request_raw=None,
        )
        update_project(project_id, {"status": STATUS_PAYMENT_PENDING, "last_job_id": job["id"]})
        await enqueue_job({"job_id": job["id"], "kind": "site_build"}, queue_name=SITE_QUEUE_NAME)
        state_clear(int(user_id))
        await _send_message(
            deps,
            chat_id,
            "⏳ Сайт создается. Я собираю структуру, тексты и ZIP-архив. Как только всё будет готово, сразу пришлю файл сюда.",
            reply_markup=site_menu_keyboard(),
        )
        return True

    if data.startswith("site:brief:retry:"):
        project_id = data.rsplit(":", 1)[-1]
        project = get_project_for_user(int(user_id), project_id)
        if not project:
            await _send_message(deps, chat_id, "Проект не найден.")
            return True
        state_set(int(user_id), STATE_WAIT_BRIEF, {"project_id": project_id})
        await _send_message(deps, chat_id, "Пришлите новый бриф одним сообщением.\n\n" + BRIEF_EXAMPLE, reply_markup=site_menu_keyboard())
        return True

    if data.startswith("site:project:"):
        project_id = data.rsplit(":", 1)[-1]
        project = get_project_for_user(int(user_id), project_id)
        if not project:
            await _send_message(deps, chat_id, "Проект не найден.")
            return True
        version = int(project.get("current_version") or 0)
        text = (
            f"🌐 {str(project.get('title') or 'Сайт')}\n"
            f"Статус: {str(project.get('status') or '-')}\n"
            f"Текущая версия: v{version}\n"
            f"Бесплатная правка: {'использована' if bool(project.get('free_revision_used')) else 'доступна'}"
        )
        await _send_message(
            deps,
            chat_id,
            text,
            reply_markup=site_project_actions_keyboard(project_id, current_version=version),
        )
        return True

    if data.startswith("site:download:"):
        parts = data.split(":")
        if len(parts) != 4:
            await _send_message(deps, chat_id, "Некорректная команда скачивания.")
            return True
        _, _, project_id, version_raw = parts
        version = get_version_for_user(int(user_id), project_id, int(version_raw))
        if not version:
            await _send_message(deps, chat_id, "Версия сайта не найдена.")
            return True
        raw = download_zip(str(version.get("zip_storage_path") or ""))
        await _tg_send_document_bytes(
            chat_id,
            raw,
            filename=f"site-v{int(version.get('version_number') or 1)}.zip",
            caption=f"Готово. Версия v{int(version.get('version_number') or 1)}",
        )
        return True

    if data.startswith("site:edit:start:"):
        project_id = data.rsplit(":", 1)[-1]
        project = get_project_for_user(int(user_id), project_id)
        if not project:
            await _send_message(deps, chat_id, "Проект не найден.")
            return True
        state_set(int(user_id), STATE_WAIT_REVISION_TEXT, {"project_id": project_id})
        await _send_message(
            deps,
            chat_id,
            "Опишите изменения одним сообщением. Лучше отправить весь пакет правок сразу одним списком.",
            reply_markup=site_menu_keyboard(),
        )
        return True

    if data.startswith("site:edit:confirm:"):
        project_id = data.rsplit(":", 1)[-1]
        project = get_project_for_user(int(user_id), project_id)
        if not project:
            await _send_message(deps, chat_id, "Проект не найден.")
            return True
        state, payload = state_get(int(user_id))
        if state != STATE_WAIT_PAID_REVISION_CONFIRM or str((payload or {}).get("project_id") or "") != project_id:
            await _send_message(deps, chat_id, "Правки не найдены. Запустите редактирование заново.")
            return True
        revision_request = str((payload or {}).get("revision_request") or "").strip()
        if not revision_request:
            await _send_message(deps, chat_id, "Текст правок пустой. Запустите редактирование заново.")
            return True
        balance = get_site_balance(int(user_id))
        if balance < SITE_REVISION_PRICE:
            state_clear(int(user_id))
            await _send_message(
                deps,
                chat_id,
                f"❌ Недостаточно токенов для правки. Нужно {SITE_REVISION_PRICE}, баланс: {balance}.",
                reply_markup=site_menu_keyboard(),
            )
            return True
        job = create_job(
            project_id=project_id,
            telegram_user_id=int(user_id),
            job_type=JOB_REVISION,
            tokens_cost=SITE_REVISION_PRICE,
            is_free_revision=False,
            request_raw=revision_request,
        )
        charge_site_revision(user_id=int(user_id), job_id=job["id"], project_id=project_id, meta={"title": project.get("title")})
        await enqueue_job({"job_id": job["id"], "kind": "site_revision"}, queue_name=SITE_QUEUE_NAME)
        state_clear(int(user_id))
        await _send_message(
            deps,
            chat_id,
            f"⏳ Правки запущены за {SITE_REVISION_PRICE} токенов. Как только новая версия будет готова, я пришлю ZIP-архив.",
            reply_markup=site_menu_keyboard(),
        )
        return True

    return False
