"""
Admin API routes — token management, registration, and system control.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from loguru import logger

from app.token_manager import TokenManager, TokenStatus


router = APIRouter(prefix="/admin")


def _get_tm(request: Request) -> TokenManager:
    return request.app.state.token_manager


@router.get("/tokens")
async def list_tokens(request: Request):
    tm = _get_tm(request)
    tokens = []
    for t in tm.tokens:
        tokens.append({
            "email": t.email,
            "status": t.status.value,
            "plan_type": t.plan_type,
            "use_count": t.use_count,
            "fail_count": t.fail_count,
            "last_fail_reason": t.last_fail_reason,
            "last_used_at": t.last_used_at,
            "cooldown_until": t.cooldown_until,
            "proxy": t.proxy,
        })
    return JSONResponse(content={"tokens": tokens, "stats": tm.get_stats()})


@router.post("/tokens")
async def add_token(request: Request):
    """Add a new token to the pool."""
    body = await request.json()
    tm = _get_tm(request)
    from app.token_manager import TokenInfo
    token = TokenInfo.from_dict(body)
    tm.add_token(token)
    return JSONResponse(content={"ok": True, "email": token.email, "status": token.status.value})


@router.post("/tokens/{email}/disable")
async def disable_token(email: str, request: Request):
    tm = _get_tm(request)
    token = tm.get_by_email(email)
    if not token:
        return JSONResponse(status_code=404, content={"error": "token not found"})
    token.status = TokenStatus.DISABLED
    token.save()
    return JSONResponse(content={"ok": True, "email": email, "status": "disabled"})


@router.post("/tokens/{email}/enable")
async def enable_token(email: str, request: Request):
    tm = _get_tm(request)
    token = tm.get_by_email(email)
    if not token:
        return JSONResponse(status_code=404, content={"error": "token not found"})
    token.status = TokenStatus.ACTIVE
    token.fail_count = 0
    token.cooldown_until = None
    token.save()
    return JSONResponse(content={"ok": True, "email": email, "status": "active"})


@router.delete("/tokens/{email}")
async def delete_token(email: str, request: Request):
    tm = _get_tm(request)
    if tm.remove_token(email):
        return JSONResponse(content={"ok": True, "email": email})
    return JSONResponse(status_code=404, content={"error": "token not found"})


@router.post("/tokens/refresh")
async def refresh_tokens(request: Request):
    tm = _get_tm(request)
    count = await tm.refresh_expired_tokens()
    return JSONResponse(content={"ok": True, "refreshed": count})


@router.post("/tokens/scan")
async def scan_tokens(request: Request):
    tm = _get_tm(request)
    count = await tm.scan_new_tokens()
    return JSONResponse(content={"ok": True, "added": count})


@router.post("/register")
async def trigger_register(request: Request):
    """Manually trigger account registration."""
    from app.reg_web import register_account, CFEmailProvider, FlowContext, BrowserFingerprint
    from app.config import get_config

    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}

    cf_url = body.get("cf_url") or get_config("register.cf_url", "")
    cf_auth = body.get("cf_auth") or get_config("register.cf_auth", "")
    cf_admin_auth = body.get("cf_admin_auth") or get_config("register.cf_admin_auth", "")
    cf_domain = body.get("cf_domain") or get_config("register.cf_domain", "")
    proxy = body.get("proxy") or get_config("register.proxy") or get_config("chatgpt.proxy", "")

    if not cf_url:
        return JSONResponse(status_code=400, content={"error": "cf_url not configured"})

    try:
        from curl_cffi import requests as curl_requests

        fp = BrowserFingerprint.chrome_windows()
        proxies = {"http": proxy, "https": proxy} if proxy else None
        session = curl_requests.Session(impersonate=fp.impersonate, proxies=proxies)
        from app.reg_web import _browser_identity_headers
        session.headers.update(_browser_identity_headers(fp.user_agent, fp=fp))

        email_provider = CFEmailProvider(
            cf_url=cf_url, cf_auth=cf_auth,
            cf_admin_auth=cf_admin_auth, cf_domain=cf_domain,
            proxies=proxies,
        )

        context = FlowContext(
            fingerprint=fp,
            redirect_uri="https://platform.openai.com/auth/callback",
            client_id="app_2SKx67EdpoN0G6j64rFvigXD",
        )

        token_data = register_account(session, context, email_provider, proxies=proxies)

        # Add to pool
        from app.token_manager import TokenInfo
        token = TokenInfo.from_dict(token_data)
        tm = _get_tm(request)
        tm.add_token(token)

        return JSONResponse(content={"ok": True, "email": token_data.get("email")})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/health")
async def health(request: Request):
    tm = _get_tm(request)
    return JSONResponse(content={
        "status": "ok",
        "tokens": tm.get_stats(),
    })


# ---------- v1 admin router (how2use 兼容接口) ----------

v1_admin_router = APIRouter(prefix="/v1/admin")


@v1_admin_router.get("/tokens/counts")
async def v1_token_counts(request: Request):
    """返回按 pool 分组的 token 统计计数（轻量，不含完整 token 数据）

    response::

      {
        "pools": {
          "default": {
            "total": 490,
            "active": 478,
            "cooling": 0,
            "expired": 0,
            "disabled": 12
          }
        }
      }
    """
    tm = _get_tm(request)
    counts = _count_by_status(tm)
    return JSONResponse(content={"pools": {"default": counts}})


def _count_by_status(tm: TokenManager) -> dict[str, int]:
    result = {"total": 0, "active": 0, "cooling": 0, "expired": 0, "disabled": 0}
    for t in tm.tokens:
        result["total"] += 1
        s = t.status.value
        if s in result:
            result[s] += 1
    return result


@v1_admin_router.get("/tokens")
async def v1_list_tokens(request: Request):
    """返回按 pool 分组的 token 快照，供 how2use 消费。

    response::

      {
        "tokens": {
          "default": [
            {
              "token": "user@example.com",
              "status": "active",
              "use_count": 42,
              "fail_count": 0,
              "last_used_at": 1715300000000
            }
          ]
        }
      }
    """
    tm = _get_tm(request)
    token_list: list[dict[str, Any]] = []
    for t in tm.tokens:
        last_used = None
        if t.last_used_at is not None:
            last_used = int(t.last_used_at * 1000)
        token_list.append({
            "token": t.email,
            "status": t.status.value,
            "use_count": t.use_count,
            "fail_count": t.fail_count,
            "last_used_at": last_used,
        })
    return JSONResponse(content={"tokens": {"default": token_list}})
