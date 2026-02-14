from fastapi import APIRouter
from app.services.socials_extract import fetch_and_extract_website_data

router = APIRouter()

@router.get("/ping")
async def ping():
    return {"ok": True, "service": "leads", "ping": "pong"}

@router.get("/extract_site")
async def extract_site(url: str):
    return await fetch_and_extract_website_data(url)
