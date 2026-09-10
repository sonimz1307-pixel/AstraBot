"""Small display images. Originals and generation inputs are never replaced.

The local cache contains only derivatives of immutable assets. Every HTTP caller
must authorize the asset before using this module, including conditional GETs.
No public bucket, database migration or provider request is involved.
"""
from __future__ import annotations

import hashlib
import io
import os
import tempfile
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageOps

SIZES = {"thumb": 480, "preview": 1280}
CACHE_DIR = Path(tempfile.gettempdir()) / "nabex-trend-display-v1"
CACHE_BYTES = 64 * 1024 * 1024
CACHE_FILES = 256
_LOCKS = tuple(threading.Lock() for _ in range(32))
_DECODE = threading.Semaphore(2)
_WRITE = threading.Lock()
_UPLOAD_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="trend-display")
# At most four small image pairs may wait for Storage, including active uploads.
_UPLOAD_SLOTS = threading.BoundedSemaphore(4)


def _sidecar(key: str, variant: str) -> str:
    return f"{key}.display-v1-{variant}.webp"


def _path(asset: dict, variant: str) -> Path:
    from app.services import trend_assets as assets
    bucket, key = assets._check_asset_location(asset)
    if variant not in SIZES or asset.get("result_type") != "image":
        raise assets.TrendAssetError("Unsupported display image")
    identity = f"v1:{bucket}:{key}:{asset.get('sha256', '')}:{variant}"
    return CACHE_DIR / (hashlib.sha256(identity.encode()).hexdigest() + ".webp")


def _read(path: Path) -> bytes | None:
    try:
        raw = path.read_bytes()
        os.utime(path, None)
        return raw
    except OSError:
        return None


def _write(path: Path, raw: bytes) -> None:
    with _WRITE:
        _write_locked(path, raw)


def _write_locked(path: Path, raw: bytes) -> None:
    CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(dir=CACHE_DIR, delete=False)
    name = handle.name
    try:
        with handle:
            handle.write(raw)
        os.replace(name, path)
        entries = sorted(CACHE_DIR.glob("*.webp"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in entries)
        while entries and (total > CACHE_BYTES or len(entries) > CACHE_FILES):
            old = entries.pop(0)
            total -= old.stat().st_size
            old.unlink(missing_ok=True)
    finally:
        Path(name).unlink(missing_ok=True)


def _render(raw: bytes, edge: int) -> bytes:
    from app.services import trend_assets as assets
    if not raw or len(raw) > assets.MAX_GENERATED_IMAGE_BYTES:
        raise assets.TrendAssetError("Display source exceeds limit")
    with Image.open(io.BytesIO(raw)) as source:
        if source.format not in {"JPEG", "PNG", "WEBP"} or source.width * source.height > assets.MAX_IMAGE_PIXELS or getattr(source, "n_frames", 1) != 1:
            raise assets.TrendAssetError("Invalid display source")
        # JPEG draft decoding reduces peak memory for large camera photos.
        source.draft("RGB", (edge, edge))
        oriented = ImageOps.exif_transpose(source)
        oriented.thumbnail((edge, edge), Image.Resampling.LANCZOS)
        alpha = oriented.mode in {"RGBA", "LA"} or "transparency" in oriented.info
        clean = oriented.convert("RGBA" if alpha else "RGB")
        clean.info.clear()
        out = io.BytesIO()
        clean.save(out, "WEBP", quality=84, method=3)
        return out.getvalue()


def display_bytes(asset: dict, variant: str, *, source: bytes | None = None) -> bytes:
    from app.services import trend_assets as assets
    path = _path(asset, variant)
    # Coalesce requests for the same file, but allow different small sidecars
    # to download concurrently. Full-resolution decoding is bounded separately.
    cached = _read(path)
    if cached is not None:
        return cached
    with _LOCKS[int(path.stem[:8], 16) % len(_LOCKS)]:
        cached = _read(path)
        if cached is not None:
            return cached
        if source is None:
            bucket, key = assets._check_asset_location(asset)
            if int(asset.get("size_bytes") or 0) > assets.MAX_GENERATED_IMAGE_BYTES:
                raise assets.TrendAssetError("Display source exceeds limit")
            # Main and background worker have separate disks. Shared sidecars
            # let main fetch only small bytes produced during new uploads/tests.
            try:
                ready = bytes(assets._client().storage.from_(bucket).download(_sidecar(key, variant)))
                if len(ready) <= 4 * 1024 * 1024:
                    with Image.open(io.BytesIO(ready)) as image:
                        if image.format == "WEBP" and max(image.size) <= SIZES[variant] and getattr(image, "n_frames", 1) == 1:
                            try:
                                _write(path, ready)
                            except OSError:
                                pass
                            return ready
            except Exception:
                # V3.1 files do not have derivatives yet. They work unchanged.
                pass
        with _DECODE:
            if source is None:
                source = bytes(assets._client().storage.from_(bucket).download(key))
            raw = _render(source, SIZES[variant])
        try:
            _write(path, raw)
        except OSError:
            # A full temporary disk must not prevent viewing an authorized file.
            pass
        return raw


def warm_display(asset: dict, raw: bytes, *, persist: bool = False) -> Future | None:
    if asset.get("result_type") == "image":
        images = {v: display_bytes(asset, v, source=raw) for v in SIZES}
        if persist:
            from app.services import trend_assets as assets
            bucket, key = assets._check_asset_location(asset)
            slots = _UPLOAD_SLOTS
            if not slots.acquire(blocking=False):
                # Local display is already ready; a different process can still
                # reconstruct it from the immutable original if this queue is full.
                return None
            try:
                storage = assets._client().storage.from_(bucket)
            except Exception:
                slots.release()
                raise
            def upload():
                try:
                    for variant, data in images.items():
                        try:
                            storage.upload(_sidecar(key, variant), data,
                                           {"content-type": "image/webp", "upsert": "false", "cache-control": "0"})
                        except Exception as exc:
                            # Repeated immutable uploads may already exist. Missing
                            # sidecars can always be rebuilt without re-generating.
                            assets.LOG.warning("Display upload deferred asset=%s variant=%s error=%s",
                                               asset.get("id"), variant, type(exc).__name__)
                finally:
                    slots.release()
            try:
                # Original bytes and the DB descriptor are already committed.
                # Optional display uploads must not delay their acknowledgement.
                return _UPLOAD_POOL.submit(upload)
            except Exception:
                slots.release()
                raise
    return None


def response(asset: dict, variant: str, request=None):
    from fastapi import Response
    raw = display_bytes(asset, variant)
    etag = '"' + hashlib.sha256(raw).hexdigest() + '"'
    headers = {"Cache-Control": "private, no-cache", "Vary": "Cookie, Authorization",
               "ETag": etag, "X-Content-Type-Options": "nosniff",
               "Content-Security-Policy": "default-src 'none'"}
    if request is not None and request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(raw, media_type="image/webp", headers=headers)
