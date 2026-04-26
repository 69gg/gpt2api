"""Image proxy — bypass estuary CDN anti-leeching."""
from __future__ import annotations

import base64
from typing import Any, Dict

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse
from loguru import logger
from curl_cffi import requests as curl_requests

router = APIRouter()

def _decode_url(encoded: str) -> str:
    try:
        padding = 4 - (len(encoded) % 4)
        if padding != 4:
            encoded += "=" * padding
        return base64.urlsafe_b64decode(encoded.encode()).decode("utf-8")
    except Exception:
        return ""

def _stream_chunks(resp):
    try:
        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                yield chunk
    except Exception:
        pass

@router.get("/p/img/{encoded_url:path}")
async def proxy_image(encoded_url: str, request: Request):
    estuary_url = _decode_url(encoded_url)
    if not estuary_url or ("estuary" not in estuary_url and "oaiusercontent" not in estuary_url and "openai" not in estuary_url):
        raise HTTPException(status_code=400, detail="Invalid image URL")

    tm = request.app.state.token_manager
    token = tm.get_available()
    if not token:
        raise HTTPException(status_code=503, detail="No available tokens")

    headers = {
        "User-Agent": token.user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.7103.112 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Referer": "https://chatgpt.com/",
        "Origin": "https://chatgpt.com",
        "Sec-Fetch-Dest": "image",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "same-origin",
    }
    if token.access_token:
        headers["Authorization"] = f"Bearer {token.access_token}"
    if token.device_id:
        headers["Oai-Device-Id"] = token.device_id

    proxy = token.get_proxy("")
    proxies = {"http": proxy, "https": proxy} if proxy else None

    try:
        resp = curl_requests.get(
            estuary_url, headers=headers, proxies=proxies,
            impersonate=token.impersonate or "chrome136", timeout=30, stream=True,
        )
        if resp.status_code != 200:
            logger.warning(f"Image proxy failed: {resp.status_code}")
            raise HTTPException(status_code=resp.status_code)

        ct = resp.headers.get("Content-Type", "image/png")
        cl = resp.headers.get("Content-Length")
        hdrs = {"Content-Length": cl} if cl else {}
        return StreamingResponse(_stream_chunks(resp), media_type=ct, headers=hdrs)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Image proxy error: {e}")
        raise HTTPException(status_code=502, detail="Proxy failed")
