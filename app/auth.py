"""
API Key authentication middleware.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.config import get_config


class AuthMiddleware(BaseHTTPMiddleware):
    """Validate API key on protected endpoints."""

    async def dispatch(self, request: Request, call_next):
        api_key = get_config("server.api_key", "sk-gpt2api")
        admin_key = get_config("server.admin_key", "admin-gpt2api")

        path = request.url.path

        # Skip auth for health check, models list, and image proxy
        if path in ("/health", "/v1/models") or path.startswith("/docs") or path.startswith("/openapi") or path.startswith("/p/img/"):
            return await call_next(request)

        # Admin endpoints require admin key
        if path.startswith("/admin/") or path.startswith("/v1/admin/"):
            key = request.headers.get("X-Admin-Key", "") or request.query_params.get("admin_key", "")
            if not key:
                auth_header = request.headers.get("Authorization", "")
                if auth_header.startswith("Bearer "):
                    key = auth_header[7:]
            if key != admin_key:
                return JSONResponse(status_code=401, content={"error": "invalid_admin_key"})
            return await call_next(request)

        # API endpoints require API key
        if path.startswith("/v1/"):
            key = request.headers.get("Authorization", "")
            if key.startswith("Bearer "):
                key = key[7:]
            if not key:
                key = request.query_params.get("key", "")

            if key != api_key:
                return JSONResponse(status_code=401, content={"error": "invalid_api_key"})

        return await call_next(request)
