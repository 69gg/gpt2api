"""
/v1/chat/completions route.
"""
from __future__ import annotations

import asyncio
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
            _stream_wrapper(adapter, body),
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


async def _stream_wrapper(adapter: OpenAIChatAdapter, body: Dict[str, Any]):
    """Bridge sync generator to async StreamingResponse.

    Runs chat_completion_stream in a background thread and yields chunks
    via an asyncio queue so the event loop is never blocked.
    """
    import queue
    import threading

    sync_q = queue.Queue()

    def _run_sync():
        try:
            for chunk in adapter.chat_completion_stream(body):
                sync_q.put(("chunk", chunk))
            sync_q.put(("done", None))
        except Exception as e:
            sync_q.put(("error", e))

    thread = threading.Thread(target=_run_sync, daemon=True)
    thread.start()

    loop = asyncio.get_event_loop()
    while True:
        kind, val = await loop.run_in_executor(None, sync_q.get)
        if kind == "done":
            break
        if kind == "error":
            raise val
        yield val
