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
from .retry import retry_call
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
        self._last_conv_id = ""

    def generate(self, prompt: str, model: str = "gpt-5-3",
                 conversation_id: str = "", attachments: Optional[List[str]] = None,
                 system_hints: Optional[List[str]] = None) -> ImageResult:
        """Generate an image using f/conversation with picture_v2 system hint.

        Flow:
        1. Bootstrap + chat-requirements
        2. f/conversation/prepare with system_hints=["picture_v2"]
        3. f/conversation with system_hints=["picture_v2"]
        4. Extract asset_pointer from SSE
        5. Poll conversation for image URL

        Args:
            attachments: List of file_ids to attach (for image editing).
            system_hints: Override system hints (default ["picture_v2"]).
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

        # 2. Prepare with picture_v2 (or custom hints)
        hints = system_hints if system_hints is not None else ["picture_v2"]
        opts = ChatOptions(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            conversation_id=conversation_id,
            system_hints=hints,
            attachments=attachments or [],
        )
        conduit_token = self.client.prepare_fchat(chat_token, proof_token, opts)

        # 3. Stream f/conversation
        result = ImageResult()
        asset_pointer = ""

        for msg in self.client.stream_fchat(chat_token, proof_token, conduit_token, opts):
            if msg.conversation_id:
                result.conversation_id = msg.conversation_id
                self._last_conv_id = msg.conversation_id
            if msg.message_id:
                result.message_id = msg.message_id
            if msg.asset_pointer:
                asset_pointer = msg.asset_pointer
            if msg.is_image and msg.image_url:
                result.image_url = msg.image_url
            if msg.finish_reason == "error":
                result.status = "error"
                return result

        # 4. Poll conversation for image URL (async image generation)
        # Image generation is asynchronous: the tool message with asset_pointer
        # may not appear until the image is fully rendered.
        if result.conversation_id and not result.image_url:
            result.image_url = self._poll_for_image(
                result.conversation_id, result.message_id, asset_pointer,
                max_wait=300,
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
                resp = retry_call(
                    curl_requests.get,
                    f"{BASE_URL}{path}",
                    headers=self.client._common_headers(path),
                    proxies=self.client._proxies,
                    impersonate=self.client._impersonate,
                    timeout=30,
                    max_retries=2, delay=2.0, backoff=2.0, label="poll-image",
                )
                if resp.status_code != 200:
                    time.sleep(3)
                    continue

                data = resp.json()
                mapping = data.get("mapping") or {}
                logger.debug(f"Poll: {len(mapping)} nodes, conv={conversation_id}")

                # Scan ALL messages for image content
                for node_id, node in mapping.items():
                    msg = node.get("message") or {}
                    content = msg.get("content") or {}
                    content_type = content.get("content_type", "")
                    parts = content.get("parts") or []
                    meta = msg.get("metadata") or {}
                    async_type = meta.get("async_task_type", "")

                    # Detect upstream image generation failure
                    if async_type == "image_gen" and content_type == "text":
                        for part in parts:
                            if isinstance(part, str) and ("error" in part.lower() or "failed" in part.lower()):
                                logger.debug(f"Poll: upstream image gen failed: {part[:80]}")
                                return ""

                    # Check for image content types
                    if content_type in ("image_asset_pointer", "multimodal_text"):
                        for part in parts:
                            if isinstance(part, dict):
                                url = part.get("image_url") or part.get("url") or ""
                                if url:
                                    logger.debug(f"Poll: found image_url in {content_type}")
                                    return url
                                ap = part.get("asset_pointer", "")
                                if ap:
                                    logger.debug(f"Poll: found asset_pointer={ap[:50]} in {content_type}")
                                    dl = self._download_asset(ap)
                                    if dl:
                                        logger.debug(f"Poll: download_url={dl[:80]}")
                                        return dl

                    # Also check any message for image URLs or asset pointers
                    for part in parts:
                        if isinstance(part, dict):
                            url = part.get("image_url") or part.get("url") or ""
                            if url and ("oaiusercontent" in url or "openai" in url):
                                return url
                            ap = part.get("asset_pointer", "")
                            if ap and (ap.startswith("file-service://") or ap.startswith("sediment://")):
                                dl_url = self._download_asset(ap)
                                if dl_url:
                                    return dl_url
                        elif isinstance(part, str):
                            if part.startswith("http") and (
                                "oaiusercontent" in part or
                                "openai" in part
                            ):
                                return part

                # Check if conversation is still generating
                # Look for async_status in the response
                async_status = data.get("async_status")
                if async_status is not None and async_status != 0:
                    # Still generating, keep polling
                    time.sleep(3)
                    continue

                # If no async_status and no image found, check if all messages are done
                all_done = True
                for node_id, node in mapping.items():
                    msg = node.get("message") or {}
                    status = msg.get("status", "")
                    if status and status not in ("finished_successfully", "in_progress"):
                        continue
                    if status == "in_progress":
                        all_done = False
                        break

                if all_done:
                    break

            except Exception as e:
                logger.debug(f"Poll error: {e}")

            time.sleep(3)

        return ""

    def _download_asset(self, asset_pointer: str) -> str:
        """Get a signed download URL from asset_pointer.

        Two formats are supported:
        - file-service://{fid} → /backend-api/files/{fid}/download
        - sediment://{fid}    → /backend-api/conversation/{cid}/attachment/{fid}/download
        Both return JSON {"download_url": "..."} with a signed CDN URL.
        """
        if not asset_pointer:
            return ""

        # Determine API endpoint based on asset_pointer prefix
        if asset_pointer.startswith("file-service://"):
            fid = asset_pointer.replace("file-service://", "")
            path = f"/backend-api/files/{fid}/download"
        elif asset_pointer.startswith("sediment://"):
            fid = asset_pointer.replace("sediment://", "")
            if not self._last_conv_id:
                logger.debug("Cannot download sediment asset without conversation_id")
                return ""
            path = f"/backend-api/conversation/{self._last_conv_id}/attachment/{fid}/download"
        else:
            # Fallback: try estuary
            path = f"/backend-api/estuary/content?id={asset_pointer}"

        try:
            resp = retry_call(
                curl_requests.get,
                f"{BASE_URL}{path}",
                headers=self.client._common_headers(path),
                proxies=self.client._proxies,
                impersonate=self.client._impersonate,
                timeout=30,
                allow_redirects=False,
                max_retries=2, delay=2.0, backoff=2.0, label="download-asset",
            )
            if resp.status_code == 200:
                data = resp.json()
                dl_url = data.get("download_url", "")
                if dl_url:
                    return dl_url
                # Fallback: if redirected, the final URL might be the image
                final_url = str(resp.url)
                if "oaiusercontent" in final_url or "openai" in final_url:
                    return final_url
            # If 302 redirect, follow to get the actual URL
            elif resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "")
                if location and ("oaiusercontent" in location or "openai" in location):
                    return location
        except Exception as e:
            logger.debug(f"Download asset error: {e}")
        return ""
