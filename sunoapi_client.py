# src/providers/sunoapi_client.py
from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

import requests


class SunoAPIError(RuntimeError):
    pass


class SunoAPIClient:
    """
    Мини-клиент для https://api.sunoapi.org

    ВАЖНО: по факту твоего теста API требует callBackUrl.
    Поэтому generate() по умолчанию берет SUNOAPI_CALLBACK_URL из env,
    либо принимает callback_url параметром.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: int = 60,
    ) -> None:
        self.api_key = api_key or os.getenv("SUNOAPI_API_KEY", "").strip()
        self.base_url = (base_url or os.getenv("SUNOAPI_BASE_URL", "https://api.sunoapi.org")).rstrip("/")
        self.timeout = timeout

        if not self.api_key:
            raise SunoAPIError("SUNOAPI_API_KEY is missing")

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
        )

    def generate(
        self,
        prompt: str,
        *,
        model: str = "V4_5ALL",
        custom_mode: bool = False,
        instrumental: bool = False,
        call_back_url: Optional[str] = None,
        title: Optional[str] = None,
        style: Optional[str] = None,
        negative_tags: Optional[str] = None,
        seed: Optional[int] = None,
        # если у их API есть ещё поля — добавим позже, сейчас минимум
    ) -> str:
        """
        Запускает генерацию.
        Возвращает task_id (строка).

        По твоему тесту callBackUrl обязателен.
        """
        cb = (call_back_url or os.getenv("SUNOAPI_CALLBACK_URL", "")).strip()
        if not cb:
            raise SunoAPIError(
                "callBackUrl is required by SunoAPI. Provide call_back_url or set SUNOAPI_CALLBACK_URL."
            )

        payload: Dict[str, Any] = {
            "prompt": prompt,
            "customMode": bool(custom_mode),
            "instrumental": bool(instrumental),
            "model": model,
            "callBackUrl": cb,
        }

        # опциональные поля (если пустые — не отправляем)
        if title:
            payload["title"] = title
        if style:
            payload["style"] = style
        if negative_tags:
            payload["negativeTags"] = negative_tags
        if seed is not None:
            payload["seed"] = int(seed)

        url = f"{self.base_url}/api/v1/generate"
        r = self.session.post(url, json=payload, timeout=self.timeout)
        data = self._safe_json(r)

        # Ожидаем формат: {"code":200,"msg":"success","data":{"taskId":"..."}}
        if r.status_code != 200:
            raise SunoAPIError(f"SunoAPI HTTP {r.status_code}: {data}")

        if data.get("code") != 200:
            raise SunoAPIError(f"SunoAPI error: {data}")

        task_id = (data.get("data") or {}).get("taskId")
        if not task_id:
            raise SunoAPIError(f"taskId missing in response: {data}")

        return str(task_id)

    def record_info(self, task_id: str) -> Dict[str, Any]:
        """
        Запрос статуса/результата по taskId (polling-метод).
        Даже если мы будем использовать callback, это полезно как резерв.
        """
        url = f"{self.base_url}/api/v1/generate/record-info"
        r = self.session.get(url, params={"taskId": task_id}, timeout=self.timeout)
        data = self._safe_json(r)

        if r.status_code != 200:
            raise SunoAPIError(f"SunoAPI HTTP {r.status_code}: {data}")

        if data.get("code") not in (200,):
            raise SunoAPIError(f"SunoAPI error: {data}")

        return data

    def wait_success(
        self,
        task_id: str,
        *,
        timeout_sec: int = 180,
        poll_interval_sec: float = 2.0,
    ) -> Dict[str, Any]:
        """
        Утилита для тестов: ждём SUCCESS по record-info.
        В проде лучше callback, но для отладки удобно.
        """
        deadline = time.time() + timeout_sec
        last: Dict[str, Any] = {}
        while time.time() < deadline:
            last = self.record_info(task_id)
            # Структура статуса зависит от их API.
            # Попробуем вытащить status в нескольких местах.
            info = last.get("data") or {}
            status = info.get("status") or info.get("taskStatus") or info.get("state")
            if status in ("SUCCESS", "SUCCEED", "COMPLETED", "DONE"):
                return last
            if status in ("FAIL", "FAILED", "ERROR"):
                raise SunoAPIError(f"Task failed: {last}")
            time.sleep(poll_interval_sec)
        raise SunoAPIError(f"Timeout waiting for task {task_id}. Last response: {last}")

    @staticmethod
    def _safe_json(resp: requests.Response) -> Dict[str, Any]:
        try:
            return resp.json()
        except Exception:
            return {"_raw": resp.text}
