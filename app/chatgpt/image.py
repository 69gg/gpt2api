"""
Image generation via chatgpt.com f/conversation endpoint.

Uses system_hints=["picture_v2"] to trigger image generation mode,
then extracts image URLs from SSE stream.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Generator, List, Optional

from curl_cffi import requests as curl_requests
from loguru import logger

from .client import ChatGPTClient, ChatOptions, ChatResult, BASE_URL
from .sse import ChatMessage, parse_sse_stream, extract_chat_messages


@dataclass
class ImageResult:
    """Result of an image generation request."""
    image_url: str = ""
    asset_pointer: str = ""
    conversation_id: str = ""
    message_id: str = ""
    revised_prompt: str = ""
    status: str = "failed"


class ImageClient:
    """Image generation client using chatgpt.com f/conversation."""

    def __init__(self, chat_client: ChatGPTClient):
        self.client = chat_client

    def generate(self, prompt: str, model: str = "gpt-5-3",
                 conversation_id: str = "") -> ImageResult:
        """Generate an image using f/conversation with picture_v2 system hint.

        Flow:
        1. Bootstrap + chat-requirements
        2. f/conversation/prepare with system_hints=["picture_v2"]
        3. f/conversation with system_hints=["picture_v2"]
        4. Extract asset_pointer from SSE
        5. Poll conversation for image URL
        """
        # 1. Bootstrap + chat-requirements
        self.client.sentinel.bootstrap()
        req_result = self.client.sentinel.get_chat_requirements()
        chat_token = req_result.token
        if not chat_token:
            raise RuntimeError("Failed to get chat_requirements token")

        proof_token = ""
        if req_result.proofofwork_required and req_result.proofofwork_seed:
            proof_token = self.client.sentinel.pow_config.solve_proof(
                req_result.proofofwork_seed, req_result.proofofwork_difficulty,
                self.client.sentinel.pow_max_iter,
            )

        # 2. Prepare with picture_v2
        opts = ChatOptions(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            conversation_id=conversation_id,
            system_hints=["picture_v2"],
        )
        conduit_token = self.client.prepare_fchat(chat_token, proof_token, opts)

        # 3. Stream f/conversation
        result = ImageResult()
        asset_pointer = ""

        for msg in self.client.stream_fchat(chat_token, proof_token, conduit_token, opts):
            if msg.conversation_id:
                result.conversation_id = msg.conversation_id
            if msg.message_id:
                result.message_id = msg.message_id
            if msg.asset_pointer:
                asset_pointer = msg.asset_pointer
            if msg.is_image and msg.image_url:
                result.image_url = msg.image_url
            if msg.finish_reason == "error":
                result.status = "error"
                return result

        # 4. If we got asset_pointer but no direct URL, poll for image
        if asset_pointer and not result.image_url:
            result.image_url = self._poll_for_image(
                result.conversation_id, result.message_id, asset_pointer
            )

        result.asset_pointer = asset_pointer
        result.status = "success" if result.image_url else "failed"
        return result

    def _poll_for_image(self, conversation_id: str, message_id: str,
                        asset_pointer: str, max_wait: int = 60) -> str:
        """Poll /backend-api/conversation/{id} for completed image URL."""
        if not conversation_id:
            return ""

        path = f"/backend-api/conversation/{conversation_id}"
        deadline = time.time() + max_wait

        while time.time() < deadline:
            try:
                resp = curl_requests.get(
                    f"{BASE_URL}{path}",
                    headers=self.client._common_headers(path),
                    proxies=self.client._proxies,
                    impersonate=self.client._impersonate,
                    timeout=30,
                )
                if resp.status_code != 200:
                    time.sleep(3)
                    continue

                data = resp.json()
                # Navigate conversation tree to find our message
                mapping = data.get("mapping") or {}
                for node_id, node in mapping.items():
                    msg = node.get("message") or {}
                    if msg.get("id") == message_id:
                        content = msg.get("content") or {}
                        parts = content.get("parts") or []
                        for part in parts:
                            if isinstance(part, dict):
                                # Check for image URL
                                url = part.get("image_url") or part.get("url") or ""
                                if url:
                                    return url
                                # Check for asset_pointer match
                                ap = part.get("asset_pointer", "")
                                if ap == asset_pointer:
                                    # Try to download via estuary
                                    return self._download_asset(ap)
                            elif isinstance(part, str):
                                if part.startswith("http") and (
                                    "oaiusercontent" in part or
                                    "openai" in part
                                ):
                                    return part

                # Check if message is still generating
                msg_node = mapping.get(message_id, {})
                msg_data = msg_node.get("message", {})
                status = msg_data.get("status", "")
                if status == "finished_successfully":
                    break
            except Exception as e:
                logger.debug(f"Poll error: {e}")

            time.sleep(3)

        return ""

    def _download_asset(self, asset_pointer: str) -> str:
        """Try to get a downloadable URL from asset_pointer via estuary."""
        if not asset_pointer:
            return ""
        try:
            path = f"/backend-api/estuary/content?id={asset_pointer}"
            resp = curl_requests.get(
                f"{BASE_URL}{path}",
                headers=self.client._common_headers(path),
                proxies=self.client._proxies,
                impersonate=self.client._impersonate,
                timeout=30,
                allow_redirects=True,
            )
            # If redirected, the final URL might be the image URL
            if resp.status_code == 200:
                final_url = str(resp.url)
                if "oaiusercontent" in final_url or "openai" in final_url:
                    return final_url
        except Exception:
            pass
        return ""
