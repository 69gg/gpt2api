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
                 sse_timeout: int = 120):
        self.token_manager = token_manager
        self.proxy = proxy
        self.turnstile_solver_url = turnstile_solver_url
        self.pow_max_iter = pow_max_iter
        self.sse_timeout = sse_timeout

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
            result = client.chat(opts)
            token.record_success()
            token.save()

            return {
                "id": f"chatcmpl-{uuid.uuid4().hex[:29]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": result.content},
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
            logger.error(f"Chat completion error for {token.email}: {e}")
            return {"error": "upstream_error", "message": error_str}

    async def chat_completion_stream(self, request: Dict[str, Any]) -> Generator[str, None, None]:
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

            for msg in client.chat_stream(opts):
                if msg.content:
                    content_chunk = {
                        "id": chat_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": 0, "delta": {"content": msg.content}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(content_chunk)}\n\n"

                if msg.finish_reason in ("stop", "error"):
                    finish_chunk = {
                        "id": chat_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                    yield f"data: {json.dumps(finish_chunk)}\n\n"
                    break

            token.record_success()
            token.save()
        except Exception as e:
            error_str = str(e)
            reason = self.token_manager.classify_error(0, error_str)
            token.record_failure(reason)
            token.save()
            logger.error(f"Stream error for {token.email}: {e}")

        yield "data: [DONE]\n\n"
