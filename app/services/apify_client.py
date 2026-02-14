from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import httpx


class ApifyError(RuntimeError):
    pass


class ApifyClient:
    """
    Minimal Apify REST client:
    - run actor
    - wait until finished
    - fetch dataset items
    """

    def __init__(self, token: str, *, timeout: float = 40.0):
        if not token:
            raise ApifyError("APIFY_TOKEN is empty")
        self.token = token
        self.timeout = timeout
        self.base = "https://api.apify.com/v2"

    def _auth_params(self) -> Dict[str, str]:
        return {"token": self.token}

    async def run_actor(self, actor_id: str, actor_input: Dict[str, Any]) -> str:
        """
        Starts actor and returns run_id.
        """
        if not actor_id:
            raise ApifyError("actor_id is empty")

        url = f"{self.base}/acts/{actor_id}/runs"
        params = {"waitForFinish": "0", **self._auth_params()}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(url, params=params, json=actor_input)
            r.raise_for_status()
            data = r.json()
        run_id = (data.get("data") or {}).get("id")
        if not run_id:
            raise ApifyError("Failed to start actor: no run id returned")
        return run_id

    async def wait_run_finished(self, run_id: str, *, poll_seconds: float = 2.0, max_wait_seconds: float = 180.0) -> Dict[str, Any]:
        """
        Polls run until SUCCEEDED/FAILED/TIMED-OUT/ABORTED.
        Returns run object (data).
        """
        url = f"{self.base}/actor-runs/{run_id}"
        deadline = asyncio.get_event_loop().time() + max_wait_seconds

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            while True:
                r = await client.get(url, params=self._auth_params())
                r.raise_for_status()
                run = (r.json().get("data") or {})
                status = (run.get("status") or "").upper()

                if status in {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}:
                    return run

                if asyncio.get_event_loop().time() > deadline:
                    raise ApifyError(f"Run timeout (>{max_wait_seconds}s). Last status={status}")

                await asyncio.sleep(poll_seconds)

    async def get_dataset_items(self, dataset_id: str, *, limit: int = 2000) -> List[Dict[str, Any]]:
        """
        Fetches dataset items (clean=true returns simplified objects).
        """
        if not dataset_id:
            return []
        url = f"{self.base}/datasets/{dataset_id}/items"
        params = {
            "clean": "true",
            "format": "json",
            "limit": str(limit),
            **self._auth_params(),
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            items = r.json()
        if not isinstance(items, list):
            return []
        return items

    async def run_actor_and_get_items(
        self,
        actor_id: str,
        actor_input: Dict[str, Any],
        *,
        max_wait_seconds: float = 240.0,
        limit: int = 2000,
    ) -> List[Dict[str, Any]]:
        run_id = await self.run_actor(actor_id, actor_input)
        run = await self.wait_run_finished(run_id, max_wait_seconds=max_wait_seconds)
        status = (run.get("status") or "").upper()
        if status != "SUCCEEDED":
            raise ApifyError(f"Actor run ended with status={status}")
        dataset_id = run.get("defaultDatasetId")
        return await self.get_dataset_items(dataset_id, limit=limit)
