"""Narrow, server-owned adapters for the Trend Workshop.

Adding a model here is an explicit compatibility decision. Admin settings can
further disable a model; they cannot invent provider payload fields.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List


MODELS = {
    "gpt_image_2_kie": {
        "key": "gpt_image_2_kie", "title": "GPT Image 2", "type": "photo",
        "provider": "gpt_image_2_kie", "model": "gpt-image-2",
        "modes": ["image_to_image", "text_to_image"], "input_types": ["image"],
        "max_references": 16, "max_input_bytes": 10 * 1024 * 1024,
        "settings": {"resolution": ["2K", "4K"], "aspect_ratio": ["1:1", "4:3", "3:4", "16:9", "21:9", "9:16"]},
        "defaults": {"resolution": "2K", "aspect_ratio": "9:16"},
    },
    "seedream_5_pro": {
        "key": "seedream_5_pro", "title": "Seedream 5.0 Pro", "type": "photo",
        "provider": "seedream_5_pro", "model": "seedream-5-pro",
        "modes": ["image_to_image", "text_to_image"], "input_types": ["image"],
        "max_references": 10, "max_input_bytes": 10 * 1024 * 1024,
        "settings": {"resolution": ["1K", "2K"], "aspect_ratio": ["1:1", "4:3", "3:4", "16:9", "9:16", "2:3", "3:2"]},
        "defaults": {"resolution": "2K", "aspect_ratio": "9:16"},
    },
    "wan3": {
        "key": "wan3", "title": "Wan 3.0", "type": "video",
        "provider": "wan3", "model": "wan3",
        "modes": ["omni_reference", "text_to_video"], "input_types": ["image"],
        "max_references": 8, "max_input_bytes": 10 * 1024 * 1024,
        "settings": {"resolution": ["480p", "720p", "1080p"], "duration": list(range(2, 31)), "aspect_ratio": ["16:9", "4:3", "1:1", "3:4", "9:16"], "enable_audio": [True, False]},
        "defaults": {"resolution": "720p", "duration": 5, "aspect_ratio": "9:16", "enable_audio": True},
    },
    "seedance25": {
        "key": "seedance25", "title": "Seedance 2.5", "type": "video",
        "provider": "seedance25", "model": "seedance25-720p",
        "modes": ["omni_reference", "text_to_video"], "input_types": ["image"],
        "max_references": 10, "max_input_bytes": 10 * 1024 * 1024,
        "settings": {"resolution": ["480p", "720p"], "duration": list(range(4, 31)), "aspect_ratio": ["1:1", "4:3", "3:4", "16:9", "9:16", "21:9"]},
        "defaults": {"resolution": "720p", "duration": 5, "aspect_ratio": "9:16"},
    },
}


class TrendRecipeError(ValueError):
    pass


def list_models() -> List[Dict[str, Any]]:
    items = [deepcopy(value) for value in MODELS.values()]
    labels = {"resolution": "Качество", "aspect_ratio": "Формат", "duration": "Длительность, сек", "enable_audio": "Звук"}
    for item in items:
        item["model_key"] = item["key"]
        item["label"] = item["title"]
        item["min_references"] = 0
        item["fields"] = [{"key": key, "label": labels[key], "type": "boolean" if key == "enable_audio" else "select", "options": values, "default": item["defaults"][key]} for key, values in item["settings"].items()]
    return items


def get_model(model_key: str) -> Dict[str, Any]:
    model = MODELS.get(str(model_key or ""))
    if model is None:
        raise TrendRecipeError("Модель пока не подключена к Мастерской.")
    return deepcopy(model)


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TrendRecipeError(f"{label}: требуется целое число.")
    return value


def validate_recipe(recipe: Dict[str, Any], slots: List[Dict[str, Any]] | None = None,
                    fixed_assets: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    if not isinstance(recipe, dict):
        raise TrendRecipeError("Некорректный рецепт.")
    unknown = set(recipe) - {"model_key", "mode", "prompt", "negative_prompt", "settings"}
    if unknown:
        raise TrendRecipeError("Неподдерживаемые параметры рецепта: " + ", ".join(sorted(unknown)))
    spec = get_model(recipe.get("model_key"))
    mode = str(recipe.get("mode") or spec["modes"][0])
    if mode not in spec["modes"]:
        raise TrendRecipeError("Этот режим модели не подключён к Мастерской.")
    prompt = str(recipe.get("prompt") or "").strip()
    if not prompt or len(prompt) > 10000:
        raise TrendRecipeError("Prompt должен содержать от 1 до 10 000 символов.")
    if recipe.get("negative_prompt"):
        raise TrendRecipeError("Подключённые модели не поддерживают отдельный negative prompt.")
    raw_settings = recipe.get("settings") or {}
    if not isinstance(raw_settings, dict) or set(raw_settings) - set(spec["settings"]):
        raise TrendRecipeError("Настройки не поддерживаются выбранной моделью.")
    settings = dict(spec["defaults"])
    for key, value in raw_settings.items():
        if key == "duration":
            _integer(value, "Длительность")
        if key == "enable_audio" and not isinstance(value, bool):
            raise TrendRecipeError("Звук должен быть включён или выключен.")
        if value not in spec["settings"][key]:
            raise TrendRecipeError(f"Недопустимое значение {key}.")
        settings[key] = value
    slots = list(slots or [])
    fixed_assets = list(fixed_assets or [])
    if len(slots) > spec["max_references"]:
        raise TrendRecipeError("Слишком много пользовательских слотов.")
    seen = set()
    positions = set()
    minimum = len(fixed_assets)
    maximum = len(fixed_assets)
    for slot in slots:
        sid = str(slot.get("id") or "")
        if not sid or len(sid) > 100 or sid in seen:
            raise TrendRecipeError("Каждый слот должен иметь уникальный id.")
        seen.add(sid)
        pos = _integer(slot.get("position", len(positions)), "Позиция слота")
        if pos < 0 or pos in positions:
            raise TrendRecipeError("Позиции слотов должны быть уникальными.")
        positions.add(pos)
        if slot.get("input_type") != "image":
            raise TrendRecipeError("В этой версии поддерживаются только изображения JPG, PNG, WEBP.")
        mapping = slot.get("provider_mapping", "references")
        if mapping not in ("references", {"parameter": "references"}):
            raise TrendRecipeError("Произвольное сопоставление параметрам провайдера запрещено.")
        min_files = _integer(slot.get("min_files", 1 if slot.get("required", True) else 0), "Минимум файлов")
        max_files = _integer(slot.get("max_files", 1), "Максимум файлов")
        if min_files < 0 or max_files < 1 or min_files > max_files or max_files > spec["max_references"]:
            raise TrendRecipeError("Недопустимое количество файлов в слоте.")
        if slot.get("required", True) and min_files < 1:
            raise TrendRecipeError("Обязательный слот должен требовать хотя бы один файл.")
        if not str(slot.get("title") or "").strip() or len(str(slot.get("title"))) > 120:
            raise TrendRecipeError("Укажите название слота длиной до 120 символов.")
        if len(str(slot.get("instruction") or "")) > 1000:
            raise TrendRecipeError("Подсказка слота слишком длинная.")
        minimum += min_files
        maximum += max_files
    for asset in fixed_assets:
        if asset.get("input_type", asset.get("asset_type", "image")) != "image":
            raise TrendRecipeError("Постоянный референс должен быть изображением.")
        if asset.get("provider_mapping", "references") not in ("references", {"parameter": "references"}):
            raise TrendRecipeError("Недопустимое сопоставление постоянного референса.")
    if maximum > spec["max_references"]:
        raise TrendRecipeError(f"Модель принимает максимум {spec['max_references']} референсов, включая постоянные.")
    if mode.startswith("text_to_") and maximum:
        raise TrendRecipeError("Текстовый режим не принимает референсы; выберите режим с изображениями.")
    if not mode.startswith("text_to_") and minimum < 1:
        raise TrendRecipeError("Рецепт должен гарантированно получать хотя бы одно изображение.")
    return {"model_key": spec["key"], "mode": mode, "prompt": prompt, "settings": settings}


def base_price(recipe: Dict[str, Any]) -> int:
    """The existing Nabex pricing functions remain the price source of truth."""
    from app.routers import web_workspace_api as ww
    spec = get_model(recipe.get("model_key"))
    settings = recipe["settings"]
    if spec["type"] == "photo":
        value = ww._workspace_image_cost(spec["provider"], recipe["mode"], resolution=settings["resolution"])
    else:
        model = "seedance25-" + settings["resolution"] if spec["provider"] == "seedance25" else spec["model"]
        value = ww._workspace_video_charge_spec(provider=spec["provider"], model=model,
            mode=recipe["mode"], duration=settings["duration"], resolution=settings["resolution"],
            enable_audio=settings.get("enable_audio", False), quality="pro",
            has_seedance_video_reference=False)["tokens"]
    if isinstance(value, bool) or int(value) <= 0:
        raise TrendRecipeError("Модель не вернула положительную базовую стоимость.")
    return int(value)
