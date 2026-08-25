from __future__ import annotations

import os
from typing import Dict, List, Tuple


# Every queue currently used by the AstraBot project except WAN3_QUEUE_NAME itself.
# Keep this centralized so Telegram, workspace API and the media worker cannot drift.
_RESERVED_QUEUE_ENVS: Tuple[Tuple[str, str], ...] = (
    ("REDIS_QUEUE_NAME", "gen"),
    ("GEN_QUEUE_NAME", "gen"),
    ("MUSIC_QUEUE_NAME", "music"),
    ("SWITCHX_QUEUE_NAME", "switchx"),
    ("GPT_IMAGE2_QUEUE_NAME", "gpt_image2"),
    ("NANO_BANANA_QUEUE_NAME", "nano_banana"),
    ("SEEDREAM_T2I_QUEUE_NAME", "seedream_t2i"),
    ("SORA_QUEUE_NAME", "sora"),
    ("TOPAZ_PHOTO_QUEUE_NAME", "topaz_photo"),
    ("TOPAZ_VIDEO_QUEUE_NAME", "topaz_video"),
    ("MIDJOURNEY_TG_QUEUE_NAME", "telegram_midjourney"),
    ("TG_UPDATE_QUEUE_NAME", "tg_update"),
    ("TG_BROADCAST_QUEUE_NAME", "tg_broadcast"),
    ("TG_CHAT_OPENAI_QUEUE_NAME", "tg_chat_openai"),
    ("TG_CHAT_CLAUDE_QUEUE_NAME", "tg_chat_claude"),
    ("TG_CHAT_FABLE_QUEUE_NAME", "tg_chat_fable"),
    ("TG_STT_QUEUE_NAME", "redactor_tg_stt"),
    ("TG_TTS_QUEUE_NAME", "workspace_tg_tts"),
    ("PARTNER_EVENTS_QUEUE_NAME", "partner_events"),
    ("SITE_QUEUE_NAME", "site"),
    ("SEEDANCE25_QUEUE_NAME", "seedance25"),
    ("KLING3_KIE_QUEUE_NAME", "kling3_kie"),
    ("WORKSPACE_MEDIA_QUEUE_NAME", "workspace_media"),
    ("WORKSPACE_GROK15_QUEUE_NAME", "workspace_grok15"),
    ("WORKSPACE_VEO_RELAX_QUEUE_NAME", "workspace_veo_relax"),
    ("WORKSPACE_IMAGE_QUEUE_NAME", "workspace_image"),
    ("WORKSPACE_GPT_IMAGE2_QUEUE_NAME", "workspace_gpt_image2"),
    ("WORKSPACE_NB2LITE_QUEUE_NAME", "workspace_nb2lite"),
    ("WORKSPACE_SEEDREAM5_QUEUE_NAME", "workspace_seedream5"),
    ("WORKSPACE_CHAT_OPENAI_QUEUE_NAME", "workspace_chat_openai"),
    ("WORKSPACE_CHAT_CLAUDE_QUEUE_NAME", "workspace_chat_claude"),
    ("WORKSPACE_CHAT_FABLE_QUEUE_NAME", "workspace_chat_fable"),
    ("WORKSPACE_VIDEO_EDIT_QUEUE", "video_edit"),
    ("VIDEO_EDITOR_V2_QUEUE", "video_editor_v2"),
)

# A few producers use these names literally. Reserve them even if a worker env is
# accidentally changed, otherwise the reliable Wan consumer can migrate/consume a
# legacy list belonging to another subsystem.
_RESERVED_LITERAL_QUEUES: Tuple[Tuple[str, str], ...] = (
    ("LEGACY_GEN_QUEUE", "gen"),
    ("LEGACY_MUSIC_QUEUE", "music"),
)


def wan3_reserved_queues() -> Dict[str, str]:
    occupied: Dict[str, str] = {}
    for label, default in _RESERVED_QUEUE_ENVS:
        value = (os.getenv(label, default) or default).strip() or default
        occupied[label] = value
    for label, value in _RESERVED_LITERAL_QUEUES:
        occupied[label] = value
    return occupied


def wan3_queue_conflicts(queue_name: str) -> List[str]:
    target = str(queue_name or "").strip()
    if not target:
        return ["WAN3_QUEUE_NAME(empty)"]
    return [label for label, value in wan3_reserved_queues().items() if target == value]


def assert_wan3_queue_isolated(queue_name: str) -> None:
    conflicts = wan3_queue_conflicts(queue_name)
    if conflicts:
        raise RuntimeError(
            f"WAN3_QUEUE_NAME={str(queue_name)!r} overlaps reserved queue(s): "
            + ", ".join(conflicts)
            + ". Wan 3.0 must use a dedicated reliable queue."
        )
