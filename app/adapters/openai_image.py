"""
OpenAI /v1/images/generations adapter — image generation via ChatGPT Web Chat.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, Optional

from loguru import logger

from app.chatgpt.client import ChatGPTClient
from app.chatgpt.image import ImageClient, ImageResult
from app.token_manager import TokenManager, TokenInfo, FailReason


class OpenAIImageAdapter:
    """Adapts OpenAI /v1/images/generations requests to ChatGPT Web image generation."""

    def __init__(self, token_manager: TokenManager, proxy: str = "",
                 turnstile_solver_url: str = "", pow_max_iter: int = 500000):
        self.token_manager = token_manager
        self.proxy = proxy
        self.turnstile_solver_url = turnstile_solver_url
        self.pow_max_iter = pow_max_iter

    def _create_client(self, token: TokenInfo) -> ImageClient:
        chat_client = ChatGPTClient(
            access_token=token.access_token,
            device_id=token.device_id,
            session_id=token.session_id,
            proxy=self.proxy,
            turnstile_solver_url=self.turnstile_solver_url,
            pow_max_iter=self.pow_max_iter,
        )
        return ImageClient(chat_client)

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
            result = client.generate(prompt, model=upstream_model)

            if result.status != "success" or not result.image_url:
                token.record_failure(FailReason.UNKNOWN)
                token.save()
                return {"error": "generation_failed", "message": "Image generation failed"}

            token.record_success()
            token.save()

            images = []
            image_data = {
                "url": result.image_url,
                "revised_prompt": result.revised_prompt or prompt,
            }
            if response_format == "b64_json":
                # Try to download and convert to base64
                try:
                    from curl_cffi import requests as curl_requests
                    import base64
                    resp = curl_requests.get(result.image_url, timeout=30)
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
            logger.error(f"Image generation error for {token.email}: {e}")
            return {"error": "upstream_error", "message": error_str}
