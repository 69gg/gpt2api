"""
/v1/messages route (Anthropic compatibility).
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse, JSONResponse

from app.adapters.anthropic import AnthropicAdapter


router = APIRouter()


def _get_adapter(request: Request) -> AnthropicAdapter:
    return request.app.state.anthropic_adapter


@router.post("/v1/messages")
async def create_message(request: Request):
    body = await request.json()
    adapter = _get_adapter(request)
    stream = body.get("stream", False)

    if stream:
        return StreamingResponse(
            _stream_wrapper(adapter, body),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        result = await adapter.create_message(body)
        if "type" in result and result["type"] == "error":
            return JSONResponse(status_code=502, content=result)
        return JSONResponse(content=result)


async def _stream_wrapper(adapter: AnthropicAdapter, body: Dict[str, Any]):
    async for chunk in adapter.create_message_stream(body):
        yield chunk
