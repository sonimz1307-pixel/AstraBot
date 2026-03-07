from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from switchx_types import ALLOWED_MAX_RESOLUTIONS, DEFAULT_ALPHA_MODE


class SwitchXError(RuntimeError):
    pass


@dataclass(slots=True)
class UploadResult:
    id: str
    upload_url: str
    beeble_uri: str


@dataclass(slots=True)
class SwitchXJobResult:
    id: str
    status: str
    progress: Optional[int]
    render_url: Optional[str]
    source_url: Optional[str]
    alpha_url: Optional[str]
    error: Optional[str]
    raw: dict[str, Any]


class SwitchXClient:
    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 60.0,
    ) -> None:
        self.api_key = (api_key or os.getenv("BEEBLE_API_KEY", "")).strip()
        self.base_url = (base_url or os.getenv("BEEBLE_BASE_URL", "https://api.modal.beeble.ai/v1")).strip().rstrip("/")
        self.timeout = timeout
        if not self.api_key:
            raise SwitchXError("BEEBLE_API_KEY not set")
        if not self.base_url:
            raise SwitchXError("BEEBLE_BASE_URL not set")

    @property
    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key}

    async def create_upload_url(self, filename: str) -> UploadResult:
        payload = {"filename": filename}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(f"{self.base_url}/uploads", json=payload, headers=self._headers)
        if r.status_code >= 300:
            raise SwitchXError(f"Create upload failed: {r.status_code} {r.text[:800]}")
        data = r.json()
        return UploadResult(
            id=str(data.get("id") or ""),
            upload_url=str(data.get("upload_url") or ""),
            beeble_uri=str(data.get("beeble_uri") or ""),
        )

    async def upload_bytes(self, upload_url: str, file_bytes: bytes, content_type: str) -> None:
        headers = {"content-type": content_type}
        async with httpx.AsyncClient(timeout=max(self.timeout, 300.0)) as client:
            r = await client.put(upload_url, content=file_bytes, headers=headers)
        if r.status_code >= 300:
            raise SwitchXError(f"Upload bytes failed: {r.status_code} {r.text[:800]}")

    async def create_and_upload(self, *, filename: str, file_bytes: bytes, content_type: str) -> UploadResult:
        upl = await self.create_upload_url(filename)
        if not upl.upload_url or not upl.beeble_uri:
            raise SwitchXError("Upload endpoint returned empty upload_url or beeble_uri")
        await self.upload_bytes(upl.upload_url, file_bytes, content_type)
        return upl

    async def start_generation(
        self,
        *,
        source_uri: str,
        reference_image_uri: str,
        prompt: str,
        alpha_mode: str = DEFAULT_ALPHA_MODE,
        max_resolution: int = 1080,
        callback_url: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        seed: Optional[int] = None,
    ) -> SwitchXJobResult:
        if max_resolution not in ALLOWED_MAX_RESOLUTIONS:
            raise SwitchXError(f"Unsupported max_resolution: {max_resolution}")

        body: dict[str, Any] = {
            "generation_type": "video",
            "source_uri": source_uri,
            "reference_image_uri": reference_image_uri,
            "prompt": prompt,
            "alpha_mode": alpha_mode,
            "max_resolution": int(max_resolution),
        }
        if callback_url:
            body["callback_url"] = callback_url
        if idempotency_key:
            body["idempotency_key"] = idempotency_key
        if seed is not None:
            body["seed"] = int(seed)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(f"{self.base_url}/switchx/generations", json=body, headers=self._headers)
        if r.status_code >= 300:
            raise SwitchXError(f"Start generation failed: {r.status_code} {r.text[:1200]}")
        return self._parse_status_response(r.json())

    async def get_status(self, job_id: str) -> SwitchXJobResult:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(f"{self.base_url}/switchx/generations/{job_id}", headers=self._headers)
        if r.status_code >= 300:
            raise SwitchXError(f"Get status failed: {r.status_code} {r.text[:800]}")
        return self._parse_status_response(r.json())

    async def wait_until_done(
        self,
        job_id: str,
        *,
        timeout_sec: int = 3600,
        poll_sec: float = 8.0,
    ) -> SwitchXJobResult:
        start = asyncio.get_event_loop().time()
        last: Optional[SwitchXJobResult] = None
        while True:
            last = await self.get_status(job_id)
            status = (last.status or "").lower().strip()
            if status in ("completed", "failed"):
                return last
            if (asyncio.get_event_loop().time() - start) > float(timeout_sec):
                raise SwitchXError(f"SwitchX timeout after {timeout_sec}s")
            await asyncio.sleep(max(2.0, poll_sec))

    def _parse_status_response(self, data: dict[str, Any]) -> SwitchXJobResult:
        output = data.get("output") or {}
        return SwitchXJobResult(
            id=str(data.get("id") or ""),
            status=str(data.get("status") or ""),
            progress=(int(data["progress"]) if data.get("progress") is not None else None),
            render_url=(output.get("render") if isinstance(output, dict) else None),
            source_url=(output.get("source") if isinstance(output, dict) else None),
            alpha_url=(output.get("alpha") if isinstance(output, dict) else None),
            error=(str(data.get("error")) if data.get("error") else None),
            raw=data,
        )


def guess_content_type(filename: str) -> str:
    ext = (filename.rsplit(".", 1)[-1].lower() if "." in filename else "").strip()
    if ext in ("jpg", "jpeg"):
        return "image/jpeg"
    if ext == "png":
        return "image/png"
    if ext == "webp":
        return "image/webp"
    if ext == "mp4":
        return "video/mp4"
    if ext == "mov":
        return "video/quicktime"
    return "application/octet-stream"
