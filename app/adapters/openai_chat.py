"""
OpenAI /v1/chat/completions adapter — converts between OpenAI format and ChatGPT Web Chat.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, Generator, List, Optional

from loguru import logger

from app.chatgpt.client import ChatGPTClient, ChatOptions, ChatResult
from app.chatgpt.sse import ChatMessage
from app.token_manager import TokenManager, TokenInfo, FailReason


# Model mapping: API model name → chatgpt.com upstream model slug
MODEL_MAP = {
    # Display IDs (dot notation)
    "gpt-5.3": "gpt-5-3",
    "gpt-5.2": "gpt-5-2",
    "gpt-5.1": "gpt-5-1",
    "gpt-5-mini": "gpt-5-mini",
    "gpt-5.3-mini": "gpt-5-3-mini",
    "gpt-5.4-mini-thinking": "gpt-5-4-t-mini",
    "auto": "auto",
    "research": "research",
    # Upstream slugs (pass-through)
    "gpt-5-3": "gpt-5-3",
    "gpt-5-2": "gpt-5-2",
    "gpt-5-1": "gpt-5-1",
    "gpt-5": "gpt-5",
    "gpt-5-3-mini": "gpt-5-3-mini",
    "gpt-5-4-t-mini": "gpt-5-4-t-mini",
    # Legacy aliases
    "gpt-4o": "auto",
    "gpt-4o-mini": "auto",
    "gpt-image-2": "gpt-5-3",
}


def _map_model(model: str) -> str:
    return MODEL_MAP.get(model, model)


def _is_image_model(model: str) -> bool:
    return "image" in model.lower()


class OpenAIChatAdapter:
    """Adapts OpenAI /v1/chat/completions requests to ChatGPT Web Chat."""

    def __init__(self, token_manager: TokenManager, proxy: str = "",
                 turnstile_solver_url: str = "", pow_max_iter: int = 500000,
                 sse_timeout: int = 120, deployment_url: str = ""):
        self.token_manager = token_manager
        self.proxy = proxy
        self.turnstile_solver_url = turnstile_solver_url
        self.pow_max_iter = pow_max_iter
        self.sse_timeout = sse_timeout
        self.deployment_url = deployment_url

    def _create_client(self, token: TokenInfo) -> ChatGPTClient:
        # Old tokens may not have user_agent/impersonate persisted.
        # Fall back to the default fingerprint used during registration.
        if not token.user_agent or not token.impersonate:
            from ..reg_web import BrowserFingerprint
            fp = BrowserFingerprint.chrome_windows()
            user_agent = token.user_agent or fp.user_agent
            impersonate = token.impersonate or getattr(fp, "impersonate", "chrome110")
        else:
            user_agent = token.user_agent
            impersonate = token.impersonate
        effective_proxy = token.get_proxy(self.proxy)
        return ChatGPTClient(
            access_token=token.access_token,
            device_id=token.device_id,
            session_id=token.session_id,
            user_agent=user_agent,
            impersonate=impersonate,
            proxy=effective_proxy,
            turnstile_solver_url=self.turnstile_solver_url,
            pow_max_iter=self.pow_max_iter,
        )

    def _poll_conv_for_image(self, client: ChatGPTClient, conv_id: str,
                             msg_id: str, max_wait: int = 120) -> str:
        """Poll /backend-api/conversation/{id} for async image generation.

        When ChatGPT generates an image via regular chat, the SSE stream
        only returns the revised prompt text. The actual image appears
        asynchronously in the conversation. This method polls until the
        image asset_pointer or URL is found.
        """
        from app.chatgpt.image import ImageClient
        from app.adapters.openai_image import _make_proxy_url

        img_client = ImageClient(client)
        img_client._last_conv_id = conv_id

        # Poll for image URL
        image_url = img_client._poll_for_image(conv_id, msg_id, "", max_wait=max_wait)

        if image_url and self.deployment_url:
            image_url = _make_proxy_url(image_url, self.deployment_url)

        return image_url

    def _resolve_image_url(self, client: ChatGPTClient, conv_id: str,
                           msg_id: str, asset_pointer: str) -> str:
        """Resolve an asset_pointer to a downloadable image URL.

        Uses ImageClient._poll_for_image and _download_asset to get the
        final signed URL, then optionally proxies it through deployment_url.
        """
        from app.chatgpt.image import ImageClient
        from app.adapters.openai_image import _make_proxy_url

        img_client = ImageClient(client)
        img_client._last_conv_id = conv_id

        image_url = ""
        # Try downloading the asset pointer directly
        if asset_pointer:
            image_url = img_client._download_asset(asset_pointer)

        # If no URL from asset, try polling the conversation
        if not image_url and conv_id:
            image_url = img_client._poll_for_image(conv_id, msg_id, asset_pointer, max_wait=120)

        if image_url and self.deployment_url:
            image_url = _make_proxy_url(image_url, self.deployment_url)

        return image_url

    def _build_messages(self, messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Convert OpenAI messages to ChatGPT format."""
        result = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                # Prepend system message as a user message with special prefix
                result.append({"role": "user", "content": f"[System Instructions]\n{content}"})
                result.append({"role": "assistant", "content": "Understood."})
            elif role in ("user", "assistant"):
                result.append({"role": role, "content": content})
            elif role == "tool":
                # Skip tool messages for web chat
                pass
        return result

    async def chat_completion(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Handle non-streaming chat completion request."""
        token = self.token_manager.get_available()
        if not token:
            return {"error": "no_available_token", "message": "No available tokens in pool"}

        client = self._create_client(token)
        model = request.get("model", "auto")
        upstream_model = _map_model(model)
        messages = self._build_messages(request.get("messages", []))

        if not messages:
            return {"error": "invalid_request", "message": "Messages cannot be empty"}

        opts = ChatOptions(
            messages=messages,
            model=upstream_model,
            sse_timeout=self.sse_timeout,
        )

        try:
            logger.info(f"Chat [{token.email}]: model={model} → upstream={upstream_model}, msgs={len(messages)}")
            result = client.chat(opts)
            logger.info(f"Chat [{token.email}]: finished, content_len={len(result.content)}, finish={result.finish_reason}, is_image={result.is_image}")

            content = result.content
            # If image was generated, resolve the image URL and append it.
            # Image generation is asynchronous: the SSE stream returns the
            # revised prompt text, but the image asset appears later in the
            # conversation. Poll for it using the conversation_id.
            image_url = result.image_url
            if not image_url and result.asset_pointer:
                image_url = self._resolve_image_url(
                    client, result.conversation_id, result.message_id, result.asset_pointer)
            if not image_url and result.conversation_id:
                logger.info(f"Chat [{token.email}]: polling conversation for async image, conv={result.conversation_id}")
                image_url = self._poll_conv_for_image(
                    client, result.conversation_id, result.message_id, max_wait=60)
            if image_url:
                logger.info(f"Chat [{token.email}]: image resolved, url={image_url[:80]}")
                content += f"\n\n![image]({image_url})"
            elif result.is_image:
                logger.warning(f"Chat [{token.email}]: image detected but URL resolution failed")

            token.record_success()
            token.save()

            return {
                "id": f"chatcmpl-{uuid.uuid4().hex[:29]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": result.finish_reason or "stop",
                }],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            }
        except Exception as e:
            error_str = str(e)
            reason = self.token_manager.classify_error(0, error_str)
            token.record_failure(reason)
            token.save()
            logger.error(f"Chat [{token.email}]: FAILED model={model} reason={reason} err={e}")
            return {"error": "upstream_error", "message": error_str}

    def chat_completion_stream(self, request: Dict[str, Any]) -> Generator[str, None, None]:
        """Handle streaming chat completion request. Yields SSE-formatted chunks."""
        token = self.token_manager.get_available()
        if not token:
            error_chunk = {
                "id": f"chatcmpl-{uuid.uuid4().hex[:29]}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": request.get("model", "auto"),
                "choices": [{"index": 0, "delta": {"content": "Error: No available tokens"},
                             "finish_reason": None}],
            }
            yield f"data: {json.dumps(error_chunk)}\n\n"
            yield "data: [DONE]\n\n"
            return

        client = self._create_client(token)
        model = request.get("model", "auto")
        upstream_model = _map_model(model)
        messages = self._build_messages(request.get("messages", []))
        chat_id = f"chatcmpl-{uuid.uuid4().hex[:29]}"
        created = int(time.time())
        logger.info(f"Stream [{token.email}]: model={model} → upstream={upstream_model}, msgs={len(messages)}")

        if not messages:
            return

        opts = ChatOptions(
            messages=messages,
            model=upstream_model,
            sse_timeout=self.sse_timeout,
        )

        try:
            # First yield role delta
            role_chunk = {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(role_chunk)}\n\n"

            content_len = 0
            conv_id = ""
            msg_id = ""
            asset_pointer = ""
            has_image = False
            image_url_direct = ""

            for msg in client.chat_stream(opts):
                if msg.content:
                    content_len += len(msg.content)
                    content_chunk = {
                        "id": chat_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": 0, "delta": {"content": msg.content}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(content_chunk)}\n\n"

                # Track image metadata from SSE
                if msg.conversation_id:
                    conv_id = msg.conversation_id
                if msg.message_id:
                    msg_id = msg.message_id
                if msg.is_image:
                    has_image = True
                    if msg.image_url:
                        image_url_direct = msg.image_url
                if msg.asset_pointer:
                    asset_pointer = msg.asset_pointer

                if msg.finish_reason in ("stop", "error"):
                    break

            # After stream ends, check for async image generation.
            # ChatGPT image gen is asynchronous: the SSE stream returns the
            # revised prompt text and finishes, but the image appears later
            # in the conversation. Poll for it if we have a conversation_id.
            image_url = image_url_direct
            if not image_url and has_image and asset_pointer:
                logger.info(f"Stream [{token.email}]: resolving image asset={asset_pointer[:50]}")
                image_url = self._resolve_image_url(client, conv_id, msg_id, asset_pointer)

            if not image_url and conv_id:
                logger.info(f"Stream [{token.email}]: polling conversation for async image, conv={conv_id}")
                image_url = self._poll_conv_for_image(client, conv_id, msg_id)

            if image_url:
                logger.info(f"Stream [{token.email}]: image resolved, url={image_url[:80]}")
                img_chunk = {
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": f"\n\n![image]({image_url})"}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(img_chunk)}\n\n"

            logger.info(f"Stream [{token.email}]: finished, content_len={content_len}, has_image={bool(image_url)}")
            finish_chunk = {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(finish_chunk)}\n\n"

            token.record_success()
            token.save()
        except Exception as e:
            error_str = str(e)
            reason = self.token_manager.classify_error(0, error_str)
            token.record_failure(reason)
            token.save()
            logger.error(f"Stream [{token.email}]: FAILED model={model} reason={reason} err={e}")

        yield "data: [DONE]\n\n"
