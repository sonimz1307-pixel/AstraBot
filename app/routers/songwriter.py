"""
AstraBot: Songwriter router for WebApp Music (lyrics helper via GPT).

Mount in main.py:
    from routers.songwriter import router as songwriter_router
    app.include_router(songwriter_router)

Endpoint:
    POST /api/songwriter_lyrics

Payload (from WebApp):
    {
      "text": "user message",
      "history": [{"role":"user|assistant","content":"..."}],
      "language": "ru|en|...",
      "genre": "...",
      "mood": "...",
      "references": "..."
    }

Response:
    {"answer": "..."}
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ai_chat import openai_chat_answer
from songwriter_prompt import SONGWRITER_SYSTEM_PROMPT


router = APIRouter(prefix="/api", tags=["songwriter"])


class ChatTurn(BaseModel):
    role: str = Field(..., description="user|assistant")
    content: str = Field(..., description="message text")


class SongwriterPayload(BaseModel):
    text: str = Field("", description="Current user message")
    history: Optional[list[ChatTurn]] = Field(default=None, description="Chat history")
    language: Optional[str] = None
    genre: Optional[str] = None
    mood: Optional[str] = None
    references: Optional[str] = None


def _system_prompt_with_context(p: SongwriterPayload) -> str:
    ctx_parts = []
    if p.language:
        ctx_parts.append(f"Язык: {p.language}")
    if p.genre:
        ctx_parts.append(f"Жанр: {p.genre}")
    if p.mood:
        ctx_parts.append(f"Настроение: {p.mood}")
    if p.references:
        ctx_parts.append(f"Референсы/вайб: {p.references}")

    if not ctx_parts:
        return SONGWRITER_SYSTEM_PROMPT

    return SONGWRITER_SYSTEM_PROMPT + "\n\nКонтекст:\n- " + "\n- ".join(ctx_parts)


@router.post("/songwriter_lyrics")
async def songwriter_lyrics(payload: SongwriterPayload, request: Request) -> Dict[str, Any]:
    user_text = (payload.text or "").strip()
    if not user_text:
        raise HTTPException(status_code=400, detail="Missing 'text'")

    history = None
    if payload.history:
        # Keep only user/assistant roles (system is not expected from WebApp)
        history = []
        for t in payload.history:
            role = (t.role or "").strip().lower()
            if role not in ("user", "assistant"):
                continue
            content = (t.content or "").strip()
            if not content:
                continue
            history.append({"role": role, "content": content})

    system_prompt = _system_prompt_with_context(payload)

    try:
        answer = await openai_chat_answer(
            user_text=user_text,
            system_prompt=system_prompt,
            history=history,
            temperature=0.6,
            max_tokens=900,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Songwriter GPT error: {e}")

    return {"answer": answer}
