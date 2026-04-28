from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

KLING3_KIE_DISPLAY_NAME = "Kling 3.0 - New"
KLING3_KIE_MODEL_SLUG = "kling-3.0-new"

# Retail tokens per generated second. 1 token = 9 ₽ in the project.
KLING3_KIE_PRICE_PER_SEC: Dict[str, Dict[bool, float]] = {
    "std": {False: 1.0, True: 1.5},
    "pro": {False: 1.5, True: 2.0},
    "4K": {False: 5.0, True: 6.0},
}

_ALLOWED_MODES = {"std", "pro", "4K"}
_ALLOWED_ASPECT_RATIOS = {"16:9", "9:16", "1:1"}
_ALLOWED_DURATIONS = set(range(3, 16))


def normalize_kling3_kie_mode(value: Any) -> str:
    text = str(value or "").strip()
    low = text.lower()
    if low in {"standard", "std", "720", "720p"}:
        return "std"
    if low in {"pro", "1080", "1080p"}:
        return "pro"
    if low in {"4k", "uhd", "2160", "2160p"}:
        return "4K"
    return "pro"


def normalize_kling3_kie_duration(value: Any, *, default: int = 5) -> int:
    try:
        duration = int(float(str(value or default).strip()))
    except Exception:
        duration = int(default)
    if duration < 3:
        return 3
    if duration > 15:
        return 15
    return duration


def normalize_kling3_kie_aspect_ratio(value: Any) -> str:
    text = str(value or "16:9").strip()
    return text if text in _ALLOWED_ASPECT_RATIOS else "16:9"


def normalize_kling3_kie_generation_mode(value: Any) -> str:
    text = str(value or "text_to_video").strip().lower()
    if text in {"i2v", "image", "image_to_video", "image2video", "image->video", "img2vid"}:
        return "image_to_video"
    if text in {"multi", "multishot", "multi_shot", "multi-shot", "multi_shots", "storyboard"}:
        return "multi_shot"
    return "text_to_video"


def normalize_kling3_kie_shots(value: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: List[Dict[str, Any]] = []
    total = 0
    for item in value:
        if not isinstance(item, dict):
            continue
        prompt = str(item.get("prompt") or "").strip()
        if not prompt:
            continue
        try:
            duration = int(float(str(item.get("duration") or 3).strip()))
        except Exception:
            duration = 3
        duration = max(1, min(12, duration))
        out.append({"prompt": prompt[:500], "duration": duration})
        total += duration
    return out


def kling3_kie_billable_seconds(*, duration: int, multi_shots: Optional[List[Dict[str, Any]]] = None) -> int:
    shots = normalize_kling3_kie_shots(multi_shots)
    if shots:
        total = sum(int(item.get("duration") or 0) for item in shots)
        return max(3, min(15, int(total or duration or 5)))
    return normalize_kling3_kie_duration(duration)


def calculate_kling3_kie_price(mode: Any, enable_audio: bool, duration: int, *, multi_shots: Optional[List[Dict[str, Any]]] = None) -> int:
    normalized_mode = normalize_kling3_kie_mode(mode)
    if normalized_mode not in _ALLOWED_MODES:
        raise ValueError("Invalid Kling 3.0 - New mode")
    seconds = kling3_kie_billable_seconds(duration=int(duration or 5), multi_shots=multi_shots)
    rate = KLING3_KIE_PRICE_PER_SEC[normalized_mode][bool(enable_audio)]
    return max(1, int(math.ceil(float(rate) * int(seconds))))


def kling3_kie_price_label(mode: Any, enable_audio: bool, duration: int, *, multi_shots: Optional[List[Dict[str, Any]]] = None) -> str:
    tokens = calculate_kling3_kie_price(mode, enable_audio, duration, multi_shots=multi_shots)
    return f"{tokens} ток."
