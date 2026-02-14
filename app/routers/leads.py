from fastapi import APIRouter, Body

from app.services.socials_extract import fetch_and_extract_website_data
from app.services.market_model_builder import build_brand_model_from_yandex_items

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
