from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from queue_redis import get_redis
from kie_claude_chat import kie_claude_summarize_dialogue

CHAT_MEMORY_PREFIX = (os.getenv("CHAT_MEMORY_PREFIX", "astrabot:chatmem") or "astrabot:chatmem").strip().rstrip(":")
AI_CHAT_HISTORY_MAX = int(os.getenv("AI_CHAT_HISTORY_MAX", "10") or "10")
AI_CHAT_TTL_SECONDS = int(os.getenv("AI_CHAT_TTL_SECONDS", "7200") or "7200")
AI_CHAT_SUMMARY_MAX_CHARS = int(os.getenv("AI_CHAT_SUMMARY_MAX_CHARS", "5000") or "5000")
AI_CHAT_SUMMARY_BATCH = int(os.getenv("AI_CHAT_SUMMARY_BATCH", "10") or "10")
AI_CHAT_PENDING_HARD_CAP = int(os.getenv("AI_CHAT_PENDING_HARD_CAP", "200") or "200")


def _tg_key(chat_id: int, user_id: int) -> str:
    return f"{CHAT_MEMORY_PREFIX}:tg:{int(chat_id)}:{int(user_id)}"


def _clean_text(value: Any, limit: int = 12000) -> str:
    text = str(value or "").replace("\x00", "").strip()
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def _sanitize_messages(value: Any, *, limit_each: int = 12000) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if not isinstance(value, list):
        return out
    for item in value:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        content = _clean_text(item.get("content"), limit_each)
        if role in {"user", "assistant"} and content:
            out.append({"role": role, "content": content})
    return out


def _normalize_memory(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, str) and raw:
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return {
        "hist": _sanitize_messages(raw.get("hist")),
        "pending": _sanitize_messages(raw.get("pending"), limit_each=1200),
        "summary": _clean_text(raw.get("summary"), AI_CHAT_SUMMARY_MAX_CHARS),
        "ts": float(raw.get("ts") or time.time()),
    }


async def get_tg_chat_memory(chat_id: int, user_id: int) -> Dict[str, Any]:
    r = await get_redis()
    raw = await r.get(_tg_key(chat_id, user_id))
    return _normalize_memory(raw)


async def save_tg_chat_memory(chat_id: int, user_id: int, memory: Dict[str, Any]) -> None:
    data = _normalize_memory(memory)
    data["hist"] = data["hist"][-max(1, AI_CHAT_HISTORY_MAX):]
    data["pending"] = data["pending"][-max(1, AI_CHAT_PENDING_HARD_CAP):]
    data["summary"] = _clean_text(data.get("summary"), AI_CHAT_SUMMARY_MAX_CHARS)
    data["ts"] = time.time()
    r = await get_redis()
    await r.set(_tg_key(chat_id, user_id), json.dumps(data, ensure_ascii=False), ex=AI_CHAT_TTL_SECONDS)


async def maybe_summarize_tg_chat_memory(chat_id: int, user_id: int) -> Dict[str, Any]:
    memory = await get_tg_chat_memory(chat_id, user_id)
    pending = _sanitize_messages(memory.get("pending"), limit_each=1200)
    if len(pending) < max(1, AI_CHAT_SUMMARY_BATCH):
        return memory

    chunk = pending[:AI_CHAT_SUMMARY_BATCH]
    rest = pending[AI_CHAT_SUMMARY_BATCH:]
    previous_summary = _clean_text(memory.get("summary"), AI_CHAT_SUMMARY_MAX_CHARS)
    try:
        summary = await kie_claude_summarize_dialogue(
            messages=chunk,
            previous_summary=previous_summary,
            max_chars=AI_CHAT_SUMMARY_MAX_CHARS,
        )
    except Exception:
        summary = previous_summary
    memory["summary"] = _clean_text(summary, AI_CHAT_SUMMARY_MAX_CHARS)
    memory["pending"] = rest
    await save_tg_chat_memory(chat_id, user_id, memory)
    return memory


async def add_tg_chat_turn(
    chat_id: int,
    user_id: int,
    *,
    user_text: str,
    assistant_text: str,
) -> Dict[str, Any]:
    memory = await get_tg_chat_memory(chat_id, user_id)
    hist = _sanitize_messages(memory.get("hist"))
    pending = _sanitize_messages(memory.get("pending"), limit_each=1200)

    new_items = []
    user_text = _clean_text(user_text, 12000)
    assistant_text = _clean_text(assistant_text, 12000)
    if user_text:
        new_items.append({"role": "user", "content": user_text})
    if assistant_text:
        new_items.append({"role": "assistant", "content": assistant_text})

    hist.extend(new_items)
    if len(hist) > max(1, AI_CHAT_HISTORY_MAX):
        overflow = hist[:-AI_CHAT_HISTORY_MAX]
        hist = hist[-AI_CHAT_HISTORY_MAX:]
        pending.extend(overflow)
    if len(pending) > max(1, AI_CHAT_PENDING_HARD_CAP):
        pending = pending[-AI_CHAT_PENDING_HARD_CAP:]

    memory["hist"] = hist
    memory["pending"] = pending
    await save_tg_chat_memory(chat_id, user_id, memory)
    return memory


async def reset_tg_chat_memory(chat_id: int, user_id: int) -> None:
    r = await get_redis()
    await r.delete(_tg_key(chat_id, user_id))
