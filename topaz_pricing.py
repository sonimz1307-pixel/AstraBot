from __future__ import annotations

import math
import os
from dataclasses import dataclass


# User-side retail setup
TOKEN_PRICE_RUB = float(os.getenv("TOKEN_PRICE_RUB", "10"))
USD_TO_RUB = float(os.getenv("USD_TO_RUB", "79.068"))
DEFAULT_SAFETY_MARGIN = float(os.getenv("TOPAZ_SAFETY_MARGIN", "1.35"))


# ------------------------
# AGREED RETAIL PRESETS
# ------------------------
PHOTO_PRESET_TOKENS = {
    "standard": 2,
    "detail": 3,
    "max": 4,
}

PHOTO_PRESET_SETTINGS = {
    "standard": {
        "enhance_model": "Standard V2",
        "upscale_factor": "2x",
        "output_format": "jpg",
        "subject_detection": "None",
        "face_enhancement": False,
        "face_enhancement_creativity": 0.0,
        "face_enhancement_strength": 0.8,
    },
    "detail": {
        "enhance_model": "High Fidelity V2",
        "upscale_factor": "4x",
        "output_format": "jpg",
        "subject_detection": "Foreground",
        "face_enhancement": False,
        "face_enhancement_creativity": 0.0,
        "face_enhancement_strength": 0.8,
    },
    "max": {
        "enhance_model": "High Fidelity V2",
        "upscale_factor": "6x",
        "output_format": "jpg",
        "subject_detection": "Foreground",
        "face_enhancement": True,
        "face_enhancement_creativity": 0.0,
        "face_enhancement_strength": 0.8,
    },
}

VIDEO_PRESET_TOKENS_PER_5S = {
    "hd_smooth": 1,       # 720p -> 720p / 60fps
    "full_hd": 2,         # 720p -> 1080p / 30fps
    "full_hd_smooth": 3,  # 720p -> 1080p / 60fps
}

VIDEO_PRESET_SETTINGS = {
    "hd_smooth": {"target_resolution": "720p", "target_fps": 60},
    "full_hd": {"target_resolution": "1080p", "target_fps": 30},
    "full_hd_smooth": {"target_resolution": "1080p", "target_fps": 60},
}


# ------------------------
# RAW REPLICATE COST TABLES
# ------------------------
# Photo model: billed by output megapixels.
PHOTO_COST_TABLE_USD = [
    (12, 0.05),
    (24, 0.05),
    (36, 0.10),
    (48, 0.10),
    (60, 0.15),
    (96, 0.20),
    (132, 0.24),
    (168, 0.29),
    (336, 0.53),
    (512, 0.82),
]

# Video model: rough guide from Replicate page.
VIDEO_ROUGH_COST_PER_5S_USD = {
    ("720p", "720p", 30): 0.027,
    ("720p", "720p", 60): 0.053,
    ("720p", "1080p", 30): 0.093,
    ("720p", "1080p", 60): 0.187,
    ("720p", "4k", 30): 0.373,
    ("720p", "4k", 60): 0.747,
}


@dataclass(slots=True)
class PriceBreakdown:
    cost_usd: float
    cost_rub: float
    retail_rub: float
    tokens: int


def usd_to_rub(usd: float, *, usd_to_rub_rate: float = USD_TO_RUB) -> float:
    return float(usd) * float(usd_to_rub_rate)


def tokens_from_retail_rub(retail_rub: float, *, token_price_rub: float = TOKEN_PRICE_RUB) -> int:
    return max(1, int(math.ceil(float(retail_rub) / float(token_price_rub))))


def build_breakdown(
    cost_usd: float,
    *,
    token_price_rub: float = TOKEN_PRICE_RUB,
    usd_to_rub_rate: float = USD_TO_RUB,
    safety_margin: float = DEFAULT_SAFETY_MARGIN,
    min_tokens: int = 1,
) -> PriceBreakdown:
    cost_rub = usd_to_rub(cost_usd, usd_to_rub_rate=usd_to_rub_rate)
    retail_rub = cost_rub * float(safety_margin)
    tokens = max(int(min_tokens), tokens_from_retail_rub(retail_rub, token_price_rub=token_price_rub))
    return PriceBreakdown(
        cost_usd=float(cost_usd),
        cost_rub=float(cost_rub),
        retail_rub=float(retail_rub),
        tokens=int(tokens),
    )


def parse_upscale_factor(value: str) -> float:
    s = str(value or "").strip().lower().replace("×", "x")
    if s.endswith("x"):
        s = s[:-1]
    factor = float(s)
    if factor <= 0:
        raise ValueError("upscale factor must be positive")
    return factor


def output_megapixels(width: int, height: int, upscale_factor: str | float) -> float:
    factor = parse_upscale_factor(str(upscale_factor))
    out_w = int(width) * factor
    out_h = int(height) * factor
    return (out_w * out_h) / 1_000_000.0


def estimate_topaz_image_cost_usd(output_mp: float) -> float:
    mp = float(output_mp)
    if mp <= 0:
        raise ValueError("output_mp must be > 0")
    for threshold_mp, cost_usd in PHOTO_COST_TABLE_USD:
        if mp <= threshold_mp:
            return float(cost_usd)
    # If user later enables very large outputs, stay conservative.
    return float(PHOTO_COST_TABLE_USD[-1][1])


def estimate_topaz_image_cost_usd_from_size(width: int, height: int, upscale_factor: str | float) -> float:
    return estimate_topaz_image_cost_usd(output_megapixels(width, height, upscale_factor))


def recommend_image_tokens_dynamic(
    width: int,
    height: int,
    upscale_factor: str | float,
    *,
    safety_margin: float = DEFAULT_SAFETY_MARGIN,
) -> PriceBreakdown:
    cost_usd = estimate_topaz_image_cost_usd_from_size(width, height, upscale_factor)
    return build_breakdown(cost_usd, safety_margin=safety_margin)


def get_photo_preset_tokens(preset_slug: str) -> int:
    slug = str(preset_slug or "").strip().lower()
    if slug not in PHOTO_PRESET_TOKENS:
        raise KeyError(f"Unknown photo preset: {preset_slug}")
    return int(PHOTO_PRESET_TOKENS[slug])


def get_photo_preset_settings(preset_slug: str) -> dict:
    slug = str(preset_slug or "").strip().lower()
    if slug not in PHOTO_PRESET_SETTINGS:
        raise KeyError(f"Unknown photo preset settings: {preset_slug}")
    return dict(PHOTO_PRESET_SETTINGS[slug])


def get_video_preset_tokens_per_5s(preset_slug: str) -> int:
    slug = str(preset_slug or "").strip().lower()
    if slug not in VIDEO_PRESET_TOKENS_PER_5S:
        raise KeyError(f"Unknown video preset: {preset_slug}")
    return int(VIDEO_PRESET_TOKENS_PER_5S[slug])


def calc_video_retail_tokens(preset_slug: str, duration_sec: int) -> int:
    per_5s = get_video_preset_tokens_per_5s(preset_slug)
    blocks = max(1, int(math.ceil(int(duration_sec) / 5.0)))
    return int(per_5s * blocks)


def estimate_video_cost_usd(
    *,
    input_resolution: str,
    target_resolution: str,
    target_fps: int,
    duration_sec: int,
) -> float:
    key = (
        str(input_resolution or "").strip().lower(),
        str(target_resolution or "").strip().lower(),
        int(target_fps),
    )
    if key not in VIDEO_ROUGH_COST_PER_5S_USD:
        raise KeyError(f"Unsupported video pricing key: {key}")
    blocks = max(1, int(math.ceil(int(duration_sec) / 5.0)))
    return float(VIDEO_ROUGH_COST_PER_5S_USD[key]) * blocks


def recommend_video_tokens_dynamic(
    *,
    input_resolution: str,
    target_resolution: str,
    target_fps: int,
    duration_sec: int,
    safety_margin: float = DEFAULT_SAFETY_MARGIN,
) -> PriceBreakdown:
    cost_usd = estimate_video_cost_usd(
        input_resolution=input_resolution,
        target_resolution=target_resolution,
        target_fps=target_fps,
        duration_sec=duration_sec,
    )
    return build_breakdown(cost_usd, safety_margin=safety_margin)


def get_video_preset_settings(preset_slug: str) -> dict:
    slug = str(preset_slug or "").strip().lower()
    if slug not in VIDEO_PRESET_SETTINGS:
        raise KeyError(f"Unknown video preset: {preset_slug}")
    return dict(VIDEO_PRESET_SETTINGS[slug])
