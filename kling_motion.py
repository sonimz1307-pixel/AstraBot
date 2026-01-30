import os
import asyncio
import aiohttp
import time
import json

REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")

TEST_IMAGE_URL = os.getenv("TEST_IMAGE_URL")
TEST_VIDEO_URL = os.getenv("TEST_VIDEO_URL")

HEADERS = {
    "Authorization": f"Bearer {REPLICATE_API_TOKEN}",
    "Content-Type": "application/json",
}


async def create_motion_control_prediction(session):
    url = "https://api.replicate.com/v1/models/kwaivgi/kling-v2.6-motion-control/predictions"

    payload = {
        "input": {
            "prompt": "A person performs the same motion as in the reference video.",
            "image": TEST_IMAGE_URL,
            "video": TEST_VIDEO_URL,
            "mode": "std",
            "character_orientation": "video",
            "keep_original_sound": True,
        }
    }

    async with session.post(url, headers=HEADERS, json=payload) as resp:
        data = await resp.json()
        return data["id"], data["urls"]["get"]


async def wait_for_result(session, get_url):
    while True:
        async with session.get(get_url, headers=HEADERS) as resp:
            data = await resp.json()

            status = data.get("status")
            print("status =", status)

            if status == "succeeded":
                return data.get("output")

            if status == "failed":
                raise RuntimeError(data.get("error"))

        await asyncio.sleep(10)


async def _selftest():
    print("Starting Kling Motion Control selftest...")

    if not TEST_IMAGE_URL or not TEST_VIDEO_URL:
        raise RuntimeError("TEST_IMAGE_URL or TEST_VIDEO_URL is not set")

    async with aiohttp.ClientSession() as session:
        prediction_id, get_url = await create_motion_control_prediction(session)
        print("Prediction ID:", prediction_id)

        output = await wait_for_result(session, get_url)

        print("OK output:", output)


if name == "main":
    asyncio.run(_selftest())
