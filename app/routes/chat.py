"""
/v1/chat/completions route.
"""
from __future__ import annotations

import json
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse, JSONResponse

from app.adapters.openai_chat import OpenAIChatAdapter


router = APIRouter()


def _get_adapter(request: Request) -> OpenAIChatAdapter:
    return request.app.state.chat_adapter


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    adapter = _get_adapter(request)
    stream = body.get("stream", False)

    if stream:
        return StreamingResponse(
            adapter.chat_completion_stream(body),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        result = await adapter.chat_completion(body)
        if "error" in result:
            return JSONResponse(status_code=502, content=result)
        return JSONResponse(content=result)
