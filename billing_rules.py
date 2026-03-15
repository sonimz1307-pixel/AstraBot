# billing_rules.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class KlingRates:
    # 1 токен = 1 секунда (STD)
    std_per_second: int = 1
    # PRO дороже: 2 токена = 1 секунда (PRO)
    pro_per_second: int = 2
    # Kling 2.5 Turbo Pro: отдельный продукт, держим фиксированную цену
    kling25_turbo_pro_per_second: int = 1

    # защита (можно потом менять)
    min_seconds: int = 1
    max_seconds: int = 60  # на старте можно поставить 10/15/30, но пусть будет 60


RATES = KlingRates()


def normalize_mode(mode: str) -> str:
    m = (mode or "").strip().lower()
    return "pro" if m in ("pro", "professional") else "std"


def normalize_product(product: Optional[str]) -> str:
    p = (product or "").strip().lower()
    aliases = {
        "kling_2_5_turbo_pro",
        "kling25",
        "kling25_turbo_pro",
        "kling-v2.5-turbo-pro",
        "kling_v2_5_turbo_pro",
    }
    return "kling_2_5_turbo_pro" if p in aliases else ""


def clamp_seconds(seconds: int) -> int:
    s = int(seconds)
    if s < RATES.min_seconds:
        s = RATES.min_seconds
    if s > RATES.max_seconds:
        s = RATES.max_seconds
    return s


def calc_kling_tokens(seconds: int, mode: str, product: Optional[str] = None) -> int:
    """
    Возвращает стоимость в токенах.

    Legacy Kling 1.6:
      STD: 1 токен/сек
      PRO: 2 токена/сек

    Kling 2.5 Turbo Pro:
      1 токен/сек (фиксированно)
    """
    s = clamp_seconds(seconds)
    product_norm = normalize_product(product)
    if product_norm == "kling_2_5_turbo_pro":
        return int(s * RATES.kling25_turbo_pro_per_second)

    m = normalize_mode(mode)
    per_sec = RATES.pro_per_second if m == "pro" else RATES.std_per_second
    return int(s * per_sec)
