"""
OpenAI /v1/images/generations adapter — image generation via ChatGPT Web Chat.
"""
from __future__ import annotations

import base64
import json
import time
import uuid
from typing import Any, Dict, Optional

from loguru import logger

from app.chatgpt.client import ChatGPTClient
from app.chatgpt.image import ImageClient, ImageResult
from app.token_manager import TokenManager, TokenInfo, FailReason


def _make_proxy_url(estuary_url: str, deployment_url: str) -> str:
    """Convert an estuary URL to a proxied URL through the deployment."""
    if not deployment_url or not estuary_url:
        return estuary_url
    encoded = base64.urlsafe_b64encode(estuary_url.encode()).decode().rstrip("=")
    return f"{deployment_url.rstrip('/')}/p/img/{encoded}"


class OpenAIImageAdapter:
    """Adapts OpenAI /v1/images/generations requests to ChatGPT Web image generation."""

    def __init__(self, token_manager: TokenManager, proxy: str = "",
                 turnstile_solver_url: str = "", pow_max_iter: int = 500000,
                 deployment_url: str = ""):
        self.token_manager = token_manager
        self.proxy = proxy
        self.turnstile_solver_url = turnstile_solver_url
        self.pow_max_iter = pow_max_iter
        self.deployment_url = deployment_url

    def _create_client(self, token: TokenInfo) -> ImageClient:
        if not token.user_agent or not token.impersonate:
            from ..reg_web import BrowserFingerprint
            fp = BrowserFingerprint.chrome_windows()
            user_agent = token.user_agent or fp.user_agent
            impersonate = token.impersonate or getattr(fp, "impersonate", "chrome110")
        else:
            user_agent = token.user_agent
            impersonate = token.impersonate
        effective_proxy = token.get_proxy(self.proxy)
        chat_client = ChatGPTClient(
            access_token=token.access_token,
            device_id=token.device_id,
            session_id=token.session_id,
            user_agent=user_agent,
            impersonate=impersonate,
            proxy=effective_proxy,
            turnstile_solver_url=self.turnstile_solver_url,
            pow_max_iter=self.pow_max_iter,
        )
        return ImageClient(chat_client)

    async def edit(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Handle image editing request.

        Accepts OpenAI /v1/images/edits format:
        - image: base64 string or URL
        - mask: base64 string or URL (optional)
        - prompt: editing instructions
        - model, size, n, response_format

        Flow:
        1. Download image (and mask) from URL if needed
        2. Upload image(s) to ChatGPT file service
        3. Send multimodal message with attachments + prompt
        4. Extract and return generated image
        """
        token = self.token_manager.get_available()
        if not token:
            return {"error": "no_available_token", "message": "No available tokens in pool"}

        prompt = request.get("prompt", "")
        if not prompt:
            return {"error": "invalid_request", "message": "Prompt cannot be empty"}

        image_input = request.get("image", "")
        mask_input = request.get("mask", "")
        model = request.get("model", "gpt-image-2")
        response_format = request.get("response_format", "url")

        if not image_input:
            return {"error": "invalid_request", "message": "Image is required for editing"}

        client = self._create_client(token)
        chat_client = client.client  # underlying ChatGPTClient

        try:
            # 1. Download image bytes (from URL or decode base64)
            def _get_bytes(data: str) -> bytes:
                if data.startswith("http://") or data.startswith("https://"):
                    effective_proxy = token.get_proxy(self.proxy)
                    proxies = {"http": effective_proxy, "https": effective_proxy} if effective_proxy else None
                    resp = curl_requests.get(
                        data, timeout=30, proxies=proxies,
                        impersonate=token.impersonate or "chrome136",
                        headers={"Referer": "https://chatgpt.com/"},
                    )
                    if resp.status_code != 200:
                        raise RuntimeError(f"Download image failed: {resp.status_code}")
                    return resp.content
                # Base64 decode
                if data.startswith("data:"):
                    data = data.split(",")[-1]
                return base64.b64decode(data)

            image_bytes = _get_bytes(image_input)

            # 2. Upload image to ChatGPT file service
            image_file_id = chat_client.upload_file(
                image_bytes, filename="image.png", mime_type="image/png")
            logger.info(f"Edit [{token.email}]: uploaded image file_id={image_file_id}")

            attachments = [image_file_id]

            # 3. Handle mask (upload as second image if provided)
            if mask_input:
                mask_bytes = _get_bytes(mask_input)
                mask_file_id = chat_client.upload_file(
                    mask_bytes, filename="mask.png", mime_type="image/png")
                logger.info(f"Edit [{token.email}]: uploaded mask file_id={mask_file_id}")
                attachments.append(mask_file_id)
                # Instruct ChatGPT to only edit the masked area
                prompt += " (only edit the white/transparent area shown in the second image)"

            # 4. Generate with attachments
            logger.info(f"Edit [{token.email}]: prompt={prompt[:60]}... attachments={len(attachments)}")
            result = client.generate(
                prompt, model="gpt-5-3", attachments=attachments)
            logger.info(f"Edit [{token.email}]: status={result.status}, url={bool(result.image_url)}")

            if result.status != "success" or not result.image_url:
                token.record_failure(FailReason.UNKNOWN)
                token.save()
                logger.warning(f"Edit [{token.email}]: generation failed, status={result.status}")
                return {"error": "generation_failed", "message": "Image editing failed"}

            token.record_success()
            token.save()

            images = []
            proxied_url = _make_proxy_url(result.image_url, self.deployment_url)
            logger.info(f"Edit [{token.email}]: url={proxied_url[:80]}...")
            image_data = {
                "url": proxied_url,
                "revised_prompt": result.revised_prompt or prompt,
            }
            if response_format == "b64_json":
                try:
                    effective_proxy = token.get_proxy(self.proxy)
                    proxies = {"http": effective_proxy, "https": effective_proxy} if effective_proxy else None
                    resp = curl_requests.get(
                        result.image_url, timeout=30, proxies=proxies,
                        impersonate=token.impersonate or "chrome136",
                        headers={"Referer": "https://chatgpt.com/"},
                    )
                    if resp.status_code == 200:
                        image_data = {
                            "b64_json": base64.b64encode(resp.content).decode("ascii"),
                            "revised_prompt": result.revised_prompt or prompt,
                        }
                except Exception:
                    pass

            images.append(image_data)

            return {
                "created": int(time.time()),
                "data": images,
            }
        except Exception as e:
            error_str = str(e)
            reason = self.token_manager.classify_error(0, error_str)
            token.record_failure(reason)
            token.save()
            logger.error(f"Edit [{token.email}]: FAILED reason={reason} err={e}")
            return {"error": "upstream_error", "message": error_str}

    async def generate(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Handle image generation request."""
        token = self.token_manager.get_available()
        if not token:
            return {"error": "no_available_token", "message": "No available tokens in pool"}

        prompt = request.get("prompt", "")
        if not prompt:
            return {"error": "invalid_request", "message": "Prompt cannot be empty"}

        model = request.get("model", "gpt-image-2")
        size = request.get("size", "1024x1024")
        n = request.get("n", 1)
        quality = request.get("quality", "auto")
        response_format = request.get("response_format", "url")

        # Map model
        upstream_model = "gpt-5-3"
        if "dall" in model.lower():
            upstream_model = "gpt-5-3"

        client = self._create_client(token)

        try:
            logger.info(f"Image [{token.email}]: prompt={prompt[:60]}... model={upstream_model}")
            result = client.generate(prompt, model=upstream_model)
            logger.info(f"Image [{token.email}]: status={result.status}, url={bool(result.image_url)}, asset={result.asset_pointer[:50] if result.asset_pointer else '-'}")

            if result.status != "success" or not result.image_url:
                token.record_failure(FailReason.UNKNOWN)
                token.save()
                logger.warning(f"Image [{token.email}]: generation failed, status={result.status}")
                return {"error": "generation_failed", "message": "Image generation failed"}

            token.record_success()
            token.save()

            images = []
            proxied_url = _make_proxy_url(result.image_url, self.deployment_url)
            logger.info(f"Image [{token.email}]: url={proxied_url[:80]}...")
            image_data = {
                "url": proxied_url,
                "revised_prompt": result.revised_prompt or prompt,
            }
            if response_format == "b64_json":
                # Try to download and convert to base64 using token proxy
                try:
                    from curl_cffi import requests as curl_requests
                    effective_proxy = token.get_proxy(self.proxy)
                    proxies = {"http": effective_proxy, "https": effective_proxy} if effective_proxy else None
                    resp = curl_requests.get(
                        result.image_url, timeout=30, proxies=proxies,
                        impersonate=token.impersonate or "chrome136",
                        headers={"Referer": "https://chatgpt.com/"},
                    )
                    if resp.status_code == 200:
                        image_data = {
                            "b64_json": base64.b64encode(resp.content).decode("ascii"),
                            "revised_prompt": result.revised_prompt or prompt,
                        }
                except Exception:
                    pass

            images.append(image_data)

            return {
                "created": int(time.time()),
                "data": images,
            }
        except Exception as e:
            error_str = str(e)
            reason = self.token_manager.classify_error(0, error_str)
            token.record_failure(reason)
            token.save()
            logger.error(f"Image [{token.email}]: FAILED reason={reason} err={e}")
            return {"error": "upstream_error", "message": error_str}
