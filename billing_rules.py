# billing_rules.py
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KlingRates:
    # 1 токен = 1 секунда (STD)
    std_per_second: int = 1
    # PRO дороже: 2 токена = 1 секунда (PRO)
    pro_per_second: int = 2

    # защита (можно потом менять)
    min_seconds: int = 1
    max_seconds: int = 60  # на старте можно поставить 10/15/30, но пусть будет 60


RATES = KlingRates()


def normalize_mode(mode: str) -> str:
    m = (mode or "").strip().lower()
    return "pro" if m in ("pro", "professional") else "std"


def clamp_seconds(seconds: int) -> int:
    s = int(seconds)
    if s < RATES.min_seconds:
        s = RATES.min_seconds
    if s > RATES.max_seconds:
        s = RATES.max_seconds
    return s


def calc_kling_tokens(seconds: int, mode: str) -> int:
    """
    Возвращает стоимость в токенах.
    STD: 1 токен/сек
    PRO: 2 токена/сек (коэффициент)
    """
    m = normalize_mode(mode)
    s = clamp_seconds(seconds)

    per_sec = RATES.pro_per_second if m == "pro" else RATES.std_per_second
    return int(s * per_sec)
