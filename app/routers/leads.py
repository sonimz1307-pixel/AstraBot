from fastapi import APIRouter, Body

from app.core.config import env
from app.services.socials_extract import fetch_and_extract_website_data
from app.services.market_model_builder import build_brand_model_from_yandex_items
from app.services.apify_client import ApifyClient, ApifyError

router = APIRouter()


@router.get("/ping")
async def ping():
    return {"ok": True, "service": "leads", "ping": "pong"}


@router.get("/extract_site")
async def extract_site(url: str):
    return await fetch_and_extract_website_data(url)


@router.post("/build_brand")
async def build_brand_endpoint(payload: dict = Body(...)):
    items = payload.get("items") or []
    return await build_brand_model_from_yandex_items(items)


@router.post("/run_apify_build_brand")
async def run_apify_build_brand(payload: dict = Body(...)):
    """
    WebApp -> backend:
    {
      "actor_id": "username~actor-name" OR "actorId",
      "actor_input": {...}    # exact actor input for Apify
    }

    Backend:
    - runs Apify actor using APIFY_TOKEN
    - downloads dataset items
    - builds unified brand model
    """
    token = env("APIFY_TOKEN")
    actor_id = (payload.get("actor_id") or "").strip()
    actor_input = payload.get("actor_input") or {}

    if not token:
        return {"ok": False, "error": "APIFY_TOKEN is not set in Render env"}
    if not actor_id:
        return {"ok": False, "error": "actor_id is required"}

    try:
        client = ApifyClient(token)
        items = await client.run_actor_and_get_items(actor_id, actor_input)
        model = await build_brand_model_from_yandex_items(items)
        model["apify"] = {"actor_id": actor_id, "items_count": len(items)}
        return model
    except ApifyError as e:
        return {"ok": False, "error": f"apify_error: {str(e)}"}
    except Exception as e:
        return {"ok": False, "error": f"server_error: {type(e).__name__}"}
