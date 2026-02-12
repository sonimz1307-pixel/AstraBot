# veo_billing.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal, Tuple

Tier = Literal["pro", "fast"]

# Фиксированная тарифная таблица (как вы согласовали)
# Veo 3.1 (pro): 2 токена/сек без звука, 3 токена/сек со звуком
# Veo 3.1 FAST (fast): 1 токен/сек без звука, 2 токена/сек со звуком
RATES = {
    ("pro", False): 2,
    ("pro", True): 3,
    ("fast", False): 1,
    ("fast", True): 2,
}

@dataclass(frozen=True)
class VeoCharge:
    tier: Tier
    generate_audio: bool
    duration_sec: int
    tokens_per_sec: int
    total_tokens: int

def _normalize_tier(veo_model: str | None, model_slug: str | None) -> Tier:
    """
    В твоём main:
      veo_model = 'pro'|'fast'
      model_slug = 'veo-3.1'|'veo-3-fast'
    Нормализуем на всякий.
    """
    vm = (veo_model or "").strip().lower()
    ms = (model_slug or "").strip().lower()

    if vm in ("pro", "3.1", "veo-3.1"):
        return "pro"
    if vm in ("fast", "std", "3-fast", "veo-3-fast"):
        return "fast"

    if "3.1" in ms:
        return "pro"
    return "fast"

def calc_veo_charge(
    *,
    veo_model: str | None,
    model_slug: str | None,
    generate_audio: bool,
    duration_sec: int,
) -> VeoCharge:
    tier: Tier = _normalize_tier(veo_model, model_slug)
    d = int(duration_sec or 0)
    if d <= 0:
        d = 8

    tps = int(RATES[(tier, bool(generate_audio))])
    total = int(d) * int(tps)
    return VeoCharge(
        tier=tier,
        generate_audio=bool(generate_audio),
        duration_sec=d,
        tokens_per_sec=tps,
        total_tokens=total,
    )

def format_veo_charge_line(ch: VeoCharge) -> str:
    tier_name = "Veo 3.1" if ch.tier == "pro" else "Veo 3.1 FAST"
    audio_txt = "со звуком" if ch.generate_audio else "без звука"
    return f"{tier_name} • {audio_txt} • {ch.duration_sec}s → {ch.total_tokens} токенов ({ch.tokens_per_sec}/сек)"
