from fastapi import APIRouter, Body

from app.services.socials_extract import fetch_and_extract_website_data
from app.services.apify_client import run_actor_sync_get_dataset_items
from app.services.market_model_builder import build_brand_model_from_yandex_items
from app.services.mi_storage import insert_raw_items, upsert_brand, replace_branches

router = APIRouter()


@router.get("/ping")
async def ping():
    return {"ok": True, "service": "leads", "ping": "pong"}


@router.get("/extract_site")
async def extract_site(url: str):
    return await fetch_and_extract_website_data(url)


@router.post("/build_brand")
async def build_brand_endpoint(payload: dict = Body(...)):
    """Build brand model from already-collected yandex/2gis-like items."""
    items = payload.get("items") or []
    return await build_brand_model_from_yandex_items(items)


@router.post("/run_apify_build_brand")
async def run_apify_build_brand(payload: dict = Body(...)):
    """1) Run Apify actor (sync) -> get dataset items.
    2) Autosave raw items into Supabase (mi_raw_items).
    3) Build a simple brand model -> try to upsert mi_brands and mi_branches.

    Expected payload:
    {
      "actor_id": "m_mamaev~2gis-places-scraper",
      "actor_input": { ... Apify actor input ... },
      "meta": {"city": "астрахань", "queries": ["школа танцев"]}
    }
    """

    actor_id = payload.get("actor_id")
    actor_input = payload.get("actor_input") or {}
    meta = payload.get("meta") or {}

    # For your 2GIS actor the city/queries are typically part of actor_input,
    # but we also accept meta.city for DB NOT NULL constraints.
    city = (meta.get("city") or actor_input.get("locationQuery") or "").strip().lower()

    # Run actor
    run_id, items = run_actor_sync_get_dataset_items(actor_id=actor_id, actor_input=actor_input)

    # Always autosave raw items (this should never break even if brand tables change)
    saved_raw = {"ok": False, "error": "not_run"}
    try:
        saved_raw = insert_raw_items(
            source="apify_2gis",
            city=city or "unknown",
            items=items,
            meta={
                "actor_id": actor_id,
                "run_id": run_id,
                "queries": meta.get("queries") or actor_input.get("query") or actor_input.get("queries"),
                "locationQuery": actor_input.get("locationQuery"),
            },
        )
    except Exception as e:
        saved_raw = {"ok": False, "error": f"raw_save_failed: {type(e).__name__}: {e}"}

    # Build model
    model = await build_brand_model_from_yandex_items(items)
    model.setdefault("meta", {})
    model["meta"].update({
        "city": city,
        "queries": meta.get("queries") or actor_input.get("query") or actor_input.get("queries"),
        "actor_id": actor_id,
        "run_id": run_id,
    })

    # Try to save brand + branches (can be switched off if you want only raw)
    saved_model = {"ok": True, "brand_id": None, "branches_inserted": 0}
    try:
        brand_name = (model.get("brand") or {}).get("name") or ""
        website = (model.get("brand") or {}).get("website") or ""
        taplink = (model.get("brand") or {}).get("taplink") or ""

        if city and brand_name:
            brand_id = upsert_brand(city=city, name=brand_name, website=website, taplink=taplink)
            branches = (model.get("brand") or {}).get("branches") or []
            br_res = replace_branches(brand_id=brand_id, branches=branches)
            saved_model = {"ok": True, "brand_id": brand_id, "branches_inserted": br_res.get("inserted", 0)}
        else:
            saved_model = {"ok": False, "error": "missing city or brand_name (skip mi_brands/mi_branches)"}

    except Exception as e:
        saved_model = {"ok": False, "error": f"model_save_failed: {type(e).__name__}: {e}"}

    return {
        "ok": True,
        "apify": {"actor_id": actor_id, "run_id": run_id, "items_count": len(items)},
        "meta": {"city": city, "queries": meta.get("queries") or actor_input.get("query") or actor_input.get("queries")},
        "saved_raw": saved_raw,
        "saved_model": saved_model,
        "brand": model.get("brand"),
    }
