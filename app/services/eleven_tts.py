import httpx


class ElevenTTS:
    def __init__(self, *, api_key: str):
        self.api_key = api_key

    async def list_voices(self):
        url = "https://api.elevenlabs.io/v1/voices"
        headers = {"xi-api-key": self.api_key}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            data = r.json()
        return data.get("voices", [])

    async def tts(self, *, text: str, voice_id: str, model_id: str, output_format: str):
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "text": text,
            "model_id": model_id,
        }

        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            return r.content
