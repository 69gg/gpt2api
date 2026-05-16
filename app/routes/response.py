"""
/v1/responses route.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse, JSONResponse

from app.adapters.openai_resp import OpenAIResponseAdapter


router = APIRouter()


def _get_adapter(request: Request) -> OpenAIResponseAdapter:
    return request.app.state.resp_adapter


@router.post("/v1/responses")
async def create_response(request: Request):
    body = await request.json()
    adapter = _get_adapter(request)
    stream = body.get("stream", False)

    if stream:
        return StreamingResponse(
            adapter.create_response_stream(body),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        result = await adapter.create_response(body)
        if "error" in result:
            return JSONResponse(status_code=502, content=result)
        return JSONResponse(content=result)
