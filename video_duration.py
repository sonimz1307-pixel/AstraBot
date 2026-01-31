# video_duration.py
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from typing import Any, Optional


def get_duration_seconds_from_telegram(message_video_obj: Optional[dict]) -> Optional[int]:
    """
    Telegram часто присылает duration для message.video (в секундах).
    Возвращает int или None.
    """
    if not isinstance(message_video_obj, dict):
        return None
    d = message_video_obj.get("duration")
    try:
        if d is None:
            return None
        d_int = int(d)
        return d_int if d_int > 0 else None
    except Exception:
        return None


def get_duration_seconds_from_bytes(video_bytes: bytes, suffix: str = ".mp4") -> int:
    """
    Fallback через ffprobe. Нужен ffprobe/ffmpeg на системе.
    Возвращает int секунд (округляем вверх).
    """
    if not video_bytes:
        raise RuntimeError("video_bytes is empty")

    ffprobe = os.getenv("FFPROBE_BIN", "ffprobe")

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
        tmp_path = f.name
        f.write(video_bytes)

    try:
        # ffprobe выводит duration в секундах (float)
        cmd = [
            ffprobe,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            tmp_path,
        ]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(f"ffprobe failed: {p.stderr.strip()[:500]}")

        data = json.loads(p.stdout or "{}")
        dur = (data.get("format") or {}).get("duration")
        if dur is None:
            raise RuntimeError("ffprobe: duration not found")

        dur_f = float(dur)
        # округление вверх, чтобы не недосписать
        sec = int(dur_f) if dur_f.is_integer() else int(dur_f) + 1
        return max(1, sec)
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


def get_duration_seconds(
    *,
    message_video_obj: Optional[dict] = None,
    video_bytes: Optional[bytes] = None,
    suffix: str = ".mp4",
) -> int:
    """
    Универсально: сначала Telegram duration, потом ffprobe.
    """
    d = get_duration_seconds_from_telegram(message_video_obj)
    if d is not None:
        return d

    if video_bytes is None:
        raise RuntimeError("Duration unknown: no telegram duration and no video_bytes provided")

    return get_duration_seconds_from_bytes(video_bytes, suffix=suffix)
