"""
/v1/models and /v1/images/generations routes.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.models import get_openai_models_response
from app.adapters.openai_image import OpenAIImageAdapter


router = APIRouter()


@router.get("/v1/models")
async def list_models():
    return JSONResponse(content=get_openai_models_response())


@router.post("/v1/images/generations")
async def create_image(request: Request):
    body = await request.json()
    adapter: OpenAIImageAdapter = request.app.state.image_adapter
    result = await adapter.generate(body)
    if "error" in result:
        return JSONResponse(status_code=502, content=result)
    return JSONResponse(content=result)


@router.post("/v1/images/edits")
async def edit_image(request: Request):
    body = await request.json()
    adapter: OpenAIImageAdapter = request.app.state.image_adapter
    result = await adapter.edit(body)
    if "error" in result:
        return JSONResponse(status_code=502, content=result)
    return JSONResponse(content=result)
