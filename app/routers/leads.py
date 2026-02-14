from fastapi import APIRouter, Body
import httpx

from app.core.config import env
from app.services.socials_extract import fetch_and_extract_website_data
from app.services.market_model_builder import build_brand_model_from_yandex_items
from app.services.apify_client import ApifyClient, ApifyError
from app.services.mi_storage import (
    insert_raw_items,
    upsert_brand,
    replace_branches,
    upsert_brand_socials,
    insert_web_snapshot,
)

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
    Runs Apify actor, builds unified model, and AUTO-SAVES into Supabase (mi_* tables).

    Body:
    {
      "actor_id": "m_mamaev~2gis-places-scraper",
      "actor_input": {...},
      "meta": {
        "source": "apify_2gis",
        "city": "астрахань",
        "queries": ["школа танцев","танцы"]
      }
    }
    """
    token = env("APIFY_TOKEN")
    actor_id = (payload.get("actor_id") or "").strip()
    actor_input = payload.get("actor_input") or {}
    meta = payload.get("meta") or {}

    if not token:
        return {"ok": False, "error": "APIFY_TOKEN is not set in Render env"}
    if not actor_id:
        return {"ok": False, "error": "actor_id is required"}

    source = (meta.get("source") or "apify").strip()
    city = (meta.get("city") or "").strip()
    queries = meta.get("queries") or []

    try:
        client = ApifyClient(token)
        # run + fetch items
        run_id = await client.run_actor(actor_id, actor_input)
        run = await client.wait_run_finished(run_id, max_wait_seconds=240.0)
        status = (run.get("status") or "").upper()
        if status != "SUCCEEDED":
            return {"ok": False, "error": f"apify_run_status:{status}", "run_id": run_id}

        dataset_id = run.get("defaultDatasetId")
        items = await client.get_dataset_items(dataset_id, limit=2000)

        # build unified model (currently expects "yandex items" shape, but works as generic branches list too)
        model = await build_brand_model_from_yandex_items(items)

        # ---- AUTO SAVE ----
        # 1) raw items
        try:
            insert_raw_items(
                source=source,
                city=city,
                queries=queries,
                actor_id=actor_id,
                run_id=run_id,
                items=items,
            )
        except Exception:
            # don't fail the whole request if logging fails
            pass

        # 2) brand + branches + socials + web snapshot
        saved = {"ok": False}
        try:
            brand_id = upsert_brand(model)
            branches_count = replace_branches(brand_id, model.get("branches") or [])
            socials_count = upsert_brand_socials(brand_id, model.get("socials") or [])
            # store website enrichment snapshot if present
            web = model.get("website_enrichment") or {}
            if isinstance(web, dict) and web.get("ok"):
                insert_web_snapshot(
                    brand_id=brand_id,
                    website=(model.get("brand") or {}).get("website") or "",
                    source_type=web.get("source_type") or "",
                    snapshot=web,
                )
            saved = {"ok": True, "brand_id": brand_id, "branches": branches_count, "socials": socials_count}
        except Exception as e:
            saved = {"ok": False, "error": type(e).__name__}

        model["apify"] = {
            "actor_id": actor_id,
            "run_id": run_id,
            "items_count": len(items),
            "dataset_id": dataset_id,
        }
        model["saved"] = saved
        return model

    except httpx.HTTPStatusError as e:
        text = ""
        try:
            text = (e.response.text or "")[:1200]
        except Exception:
            text = ""
        return {"ok": False, "error": "apify_http_error", "status_code": getattr(e.response, "status_code", None), "response": text}

    except httpx.HTTPError as e:
        return {"ok": False, "error": f"apify_network_error: {type(e).__name__}"}

    except ApifyError as e:
        return {"ok": False, "error": f"apify_error: {str(e)}"}

    except Exception as e:
        return {"ok": False, "error": f"server_error: {type(e).__name__}"}
