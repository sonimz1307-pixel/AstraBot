# piapi_suno.py
from __future__ import annotations

import os
import time
import asyncio
from typing import Any, Dict, Optional

import httpx

PIAPI_API_KEY = os.getenv("PIAPI_API_KEY", "")
PIAPI_BASE_URL = os.getenv("PIAPI_BASE_URL", "https://api.piapi.ai")

class PiAPITimeout(Exception):
    pass

class PiAPIJobFailed(Exception):
    pass


def _headers(api_key: Optional[str] = None) -> Dict[str, str]:
    key = api_key or PIAPI_API_KEY
    if not key:
        raise RuntimeError("PIAPI_API_KEY is not set")
    return {"X-API-Key": key, "Content-Type": "application/json"}


async def create_suno_music_task(
    *,
    music_mode: str,  # 'prompt' or 'custom'
    mv: str = "chirp-crow",
    title: str = "",
    tags: str = "",
    make_instrumental: bool = False,
    gpt_description_prompt: str = "",
    prompt: str = "",
    service_mode: str = "",   # 'public' | 'private' | ''
    webhook_endpoint: str = "",
    webhook_secret: str = "",
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Create Suno music task via PiAPI. Returns {task_id, status, ...}."""
    body: Dict[str, Any] = {
        "model": "suno",
        "task_type": "music",
        "input": {
            "mv": mv,
            "title": title,
            "tags": tags,
            "make_instrumental": bool(make_instrumental),
        },
    }

    if music_mode == "prompt":
        body["input"]["gpt_description_prompt"] = gpt_description_prompt
    else:
        body["input"]["prompt"] = prompt

    config: Dict[str, Any] = {}
    if service_mode in ("public", "private"):
        config["service_mode"] = service_mode
    if webhook_endpoint:
        config["webhook_config"] = {"endpoint": webhook_endpoint, "secret": webhook_secret or ""}
    if config:
        body["config"] = config

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{PIAPI_BASE_URL}/api/v1/task", headers=_headers(api_key), json=body)
        r.raise_for_status()
        return r.json()



async def create_udio_music_task(
    *,
    gpt_description_prompt: str,
    tags: str = "",
    negative_tags: str = "",
    lyrics_type: str = "lyrics",  # 'lyrics'|'instrumental'
    seed: Optional[int] = None,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Create Udio-like music task via PiAPI. Returns {task_id, status, ...}."""
    inp: Dict[str, Any] = {
        "gpt_description_prompt": gpt_description_prompt,
        "lyrics_type": lyrics_type,
    }
    if tags:
        inp["tags"] = tags
    if negative_tags:
        inp["negative_tags"] = negative_tags
    if seed is not None:
        inp["seed"] = int(seed)

    body: Dict[str, Any] = {
        "model": "music-u",
        "task_type": "generate_music",
        "input": inp,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{PIAPI_BASE_URL}/api/v1/task", headers=_headers(api_key), json=body)
        r.raise_for_status()
        return r.json()

async def get_task(task_id: str, api_key: Optional[str] = None) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{PIAPI_BASE_URL}/api/v1/task/{task_id}", headers=_headers(api_key))
        r.raise_for_status()
        return r.json()


async def poll_task_until_done(
    task_id: str,
    *,
    timeout_sec: int = 900,
    interval_sec: float = 2.5,
    backoff: float = 1.15,
    max_interval: float = 10.0,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    start = time.time()
    sleep_s = interval_sec

    while True:
        if time.time() - start > timeout_sec:
            raise PiAPITimeout(f"Timeout waiting task {task_id}")

        payload = await get_task(task_id, api_key=api_key)
        data = (payload or {}).get("data") or {}
        status = (data.get("status") or "").lower()

        if status in ("completed", "success", "succeeded", "done"):
            return payload

        if status in ("failed", "error", "canceled", "cancelled"):
            err = (data.get("error") or {}).get("message") or (payload or {}).get("message") or "unknown"
            raise PiAPIJobFailed(err)

        await asyncio.sleep(sleep_s)
        sleep_s = min(max_interval, sleep_s * backoff)


def extract_audio_url(task_payload: Dict[str, Any]) -> Optional[str]:
    """Try to extract audio url from PiAPI unified response."""
    data = (task_payload or {}).get("data") or {}
    out = data.get("output") or {}
    # common variants
    for key in ("audio_url", "audio", "audioUrl", "song_url", "songUrl", "url"):
        if isinstance(out, dict) and out.get(key):
            return out.get(key)
    # sometimes it's a list
    for key in ("audio_urls", "audios", "urls"):
        val = out.get(key) if isinstance(out, dict) else None
        if isinstance(val, list) and val:
            return val[0]
    return None
