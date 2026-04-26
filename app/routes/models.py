"""
/v1/models and /v1/images/generations routes.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Request, File, Form, UploadFile
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
async def edit_image(
    image: UploadFile = File(...),
    prompt: str = Form(...),
    mask: Optional[UploadFile] = File(None),
    model: str = Form("gpt-image-2"),
    n: int = Form(1),
    size: str = Form("1024x1024"),
    response_format: str = Form("url"),
    request: Request = None,
):
    adapter: OpenAIImageAdapter = request.app.state.image_adapter

    image_bytes = await image.read()
    mask_bytes = await mask.read() if mask else None

    result = await adapter.edit(
        image_bytes=image_bytes,
        mask_bytes=mask_bytes,
        prompt=prompt,
        model=model,
        n=n,
        size=size,
        response_format=response_format,
    )
    if "error" in result:
        return JSONResponse(status_code=502, content=result)
    return JSONResponse(content=result)
