"""
Anthropic /v1/messages adapter — converts between Anthropic format and OpenAI format,
then delegates to OpenAI chat adapter.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, Generator, List, Optional

from app.adapters.openai_chat import OpenAIChatAdapter, _map_model


# Anthropic model → OpenAI model mapping
ANTHROPIC_MODEL_MAP = {
    "claude-sonnet-4-20250514": "gpt-5.3-codex",
    "claude-3-5-sonnet-20241022": "gpt-5.2-codex",
    "claude-3-5-haiku-20241022": "gpt-4o-mini",
    "claude-3-opus-20240229": "gpt-5.3-codex",
    "claude-3-haiku-20240307": "gpt-4o-mini",
}


class AnthropicAdapter:
    """Adapts Anthropic /v1/messages requests to OpenAI chat completions."""

    def __init__(self, chat_adapter: OpenAIChatAdapter):
        self.chat_adapter = chat_adapter

    def _convert_messages(self, messages: List[Dict], system: str = "") -> List[Dict[str, str]]:
        """Convert Anthropic messages format to OpenAI format."""
        result = []
        if system:
            result.append({"role": "system", "content": system})

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if isinstance(content, list):
                # Anthropic content blocks
                text_parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") == "image":
                            # Skip images for web chat
                            pass
                    elif isinstance(block, str):
                        text_parts.append(block)
                content = "\n".join(text_parts)

            if role in ("user", "assistant"):
                result.append({"role": role, "content": content})

        return result

    def _map_model(self, model: str) -> str:
        return ANTHROPIC_MODEL_MAP.get(model, "gpt-5.2-codex")

    async def create_message(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Handle non-streaming Anthropic Messages request."""
        model = request.get("model", "claude-sonnet-4-20250514")
        messages = request.get("messages", [])
        system = request.get("system", "")
        max_tokens = request.get("max_tokens", 4096)

        openai_messages = self._convert_messages(messages, system)
        openai_model = self._map_model(model)

        chat_request = {
            "model": openai_model,
            "messages": openai_messages,
            "stream": False,
        }

        result = await self.chat_adapter.chat_completion(chat_request)
        if "error" in result:
            return {"type": "error", "error": {"type": "api_error", "message": result.get("message", "Unknown error")}}

        content_text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        return {
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": content_text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
            },
        }

    async def create_message_stream(self, request: Dict[str, Any]) -> Generator[str, None, None]:
        """Handle streaming Anthropic Messages request."""
        model = request.get("model", "claude-sonnet-4-20250514")
        messages = request.get("messages", [])
        system = request.get("system", "")
        max_tokens = request.get("max_tokens", 4096)

        openai_messages = self._convert_messages(messages, system)
        openai_model = self._map_model(model)

        chat_request = {
            "model": openai_model,
            "messages": openai_messages,
            "stream": True,
        }

        msg_id = f"msg_{uuid.uuid4().hex[:24]}"

        # Message start event
        start_event = {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        }
        yield f"event: message_start\ndata: {json.dumps(start_event)}\n\n"

        # Content block start
        content_start = {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        }
        yield f"event: content_block_start\ndata: {json.dumps(content_start)}\n\n"

        # Stream text deltas
        async for chunk in self.chat_adapter.chat_completion_stream(chat_request):
            if chunk.startswith("data: ") and chunk.strip() != "data: [DONE]":
                try:
                    data = json.loads(chunk[6:].strip())
                    delta = data.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        text_event = {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": content},
                        }
                        yield f"event: content_block_delta\ndata: {json.dumps(text_event)}\n\n"
                except (json.JSONDecodeError, IndexError):
                    pass

        # Content block stop
        content_stop = {"type": "content_block_stop", "index": 0}
        yield f"event: content_block_stop\ndata: {json.dumps(content_stop)}\n\n"

        # Message stop
        msg_stop = {
            "type": "message_stop",
        }
        yield f"event: message_stop\ndata: {json.dumps(msg_stop)}\n\n"
