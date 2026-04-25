"""
FastAPI application — route registration, lifespan, and background tasks.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from curl_cffi import requests as curl_requests
from loguru import logger

from app.config import load_config, get_config
from app.auth import AuthMiddleware
from app.token_manager import TokenManager
from app.adapters.openai_chat import OpenAIChatAdapter
from app.adapters.openai_resp import OpenAIResponseAdapter
from app.adapters.anthropic import AnthropicAdapter
from app.adapters.openai_image import OpenAIImageAdapter
from app.routes import chat, response, messages, models, admin, proxy


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown."""
    # Startup
    config_path = str(Path(__file__).parent.parent / "config.yaml")
    load_config(config_path)

    proxy = get_config("chatgpt.proxy", "")
    turnstile_solver_url = get_config("chatgpt.turnstile_solver_url", "")
    pow_max_iter = get_config("chatgpt.pow_max_iter", 500000)
    sse_timeout = get_config("chatgpt.sse_timeout", 120)
    token_dir = str(Path(__file__).parent.parent / "web_token")

    # Initialize token manager
    tm = TokenManager(
        token_dir=token_dir,
        proxy=proxy,
        turnstile_solver_url=turnstile_solver_url,
    )
    tm.load()

    # Initialize adapters
    chat_adapter = OpenAIChatAdapter(
        token_manager=tm, proxy=proxy,
        turnstile_solver_url=turnstile_solver_url,
        pow_max_iter=pow_max_iter, sse_timeout=sse_timeout,
    )
    resp_adapter = OpenAIResponseAdapter(chat_adapter)
    anthropic_adapter = AnthropicAdapter(chat_adapter)
    deployment_url = get_config("server.deployment_url", "")
    image_adapter = OpenAIImageAdapter(
        token_manager=tm, proxy=proxy,
        turnstile_solver_url=turnstile_solver_url,
        pow_max_iter=pow_max_iter,
        deployment_url=deployment_url,
    )

    # Store in app state
    app.state.token_manager = tm
    app.state.chat_adapter = chat_adapter
    app.state.resp_adapter = resp_adapter
    app.state.anthropic_adapter = anthropic_adapter
    app.state.image_adapter = image_adapter
    app.state.deployment_url = get_config("server.deployment_url", "")

    # Start background tasks
    tasks = []

    refresh_hours = get_config("token.refresh_interval_hours", 2)
    async def _refresh_loop():
        while True:
            try:
                await asyncio.sleep(refresh_hours * 3600)
                count = await tm.refresh_expired_tokens()
                if count:
                    logger.info(f"Background refresh: {count} tokens refreshed")
            except Exception as e:
                logger.error(f"Refresh loop error: {e}")

    async def _cooling_loop():
        while True:
            try:
                await asyncio.sleep(600)  # 10 minutes
                count = await tm.check_cooling_tokens()
                if count:
                    logger.info(f"Cooling recovery: {count} tokens reactivated")
            except Exception as e:
                logger.error(f"Cooling loop error: {e}")

    async def _scan_loop():
        while True:
            try:
                await asyncio.sleep(30)
                count = await tm.scan_new_tokens()
                if count:
                    logger.info(f"Token scan: {count} new tokens found")
            except Exception as e:
                logger.error(f"Scan loop error: {e}")

    async def _cleanup_loop():
        while True:
            try:
                await asyncio.sleep(3600)  # 1 hour
                count = await tm.cleanup_dead_tokens()
                if count:
                    logger.info(f"Cleanup: {count} dead tokens removed")
            except Exception as e:
                logger.error(f"Cleanup loop error: {e}")

    async def _auto_register_loop():
        """Auto-register new accounts when active tokens < min_tokens."""
        while True:
            try:
                interval = get_config("register.register_interval", 60)
                await asyncio.sleep(interval)
                auto = get_config("register.auto_register", False)
                if not auto:
                    continue
                min_tokens = get_config("register.min_tokens", 3)
                if tm.active_count >= min_tokens:
                    continue
                need = min_tokens - tm.active_count
                logger.info(f"Auto-register: {tm.active_count} active < {min_tokens} min, registering {need} account(s)")

                from app.reg_web import register_account, CFEmailProvider, FlowContext, BrowserFingerprint

                cf_url = get_config("register.cf_url", "")
                cf_auth = get_config("register.cf_auth", "")
                cf_admin_auth = get_config("register.cf_admin_auth", "")
                cf_domain = get_config("register.cf_domain", "")
                reg_proxy = get_config("register.proxy", "") or proxy

                if not cf_url:
                    logger.warning("Auto-register: cf_url not configured, skipping")
                    continue

                for i in range(need):
                    try:
                        fp = BrowserFingerprint.chrome_windows()
                        proxies = {"http": reg_proxy, "https": reg_proxy} if reg_proxy else None
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
                        from app.token_manager import TokenInfo
                        token = TokenInfo.from_dict(token_data)
                        tm.add_token(token)
                        logger.info(f"Auto-register OK: {token.email}")
                    except Exception as e:
                        logger.error(f"Auto-register failed ({i+1}/{need}): {e}")
                    await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Auto-register loop error: {e}")

    tasks.append(asyncio.create_task(_refresh_loop()))
    tasks.append(asyncio.create_task(_cooling_loop()))
    tasks.append(asyncio.create_task(_scan_loop()))
    tasks.append(asyncio.create_task(_cleanup_loop()))
    tasks.append(asyncio.create_task(_auto_register_loop()))

    logger.info(f"gpt2api started: {tm.active_count}/{tm.total_count} active tokens")

    yield

    # Shutdown
    for t in tasks:
        t.cancel()
    logger.info("gpt2api shutdown")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="gpt2api",
        description="OpenAI-compatible API backed by ChatGPT Web Chat",
        version="1.0.0",
        lifespan=lifespan,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Auth middleware
    app.add_middleware(AuthMiddleware)

    # Register routes
    app.include_router(chat.router)
    app.include_router(response.router)
    app.include_router(messages.router)
    app.include_router(models.router)
    app.include_router(admin.router)
    app.include_router(proxy.router)

    # Health check
    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app
