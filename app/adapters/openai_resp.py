"""
OpenAI /v1/responses adapter — converts between OpenAI Responses format and ChatGPT Web Chat.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncGenerator, Dict, Generator, List, Optional

from loguru import logger

from app.chatgpt.client import ChatGPTClient, ChatOptions
from app.token_manager import TokenManager, TokenInfo
from .openai_chat import OpenAIChatAdapter, _map_model


class OpenAIResponseAdapter:
    """Adapts OpenAI /v1/responses requests to ChatGPT Web Chat."""

    def __init__(self, chat_adapter: OpenAIChatAdapter):
        self.chat_adapter = chat_adapter

    def _convert_input(self, input_data: Any) -> List[Dict[str, str]]:
        """Convert Responses API input to chat messages."""
        if isinstance(input_data, str):
            return [{"role": "user", "content": input_data}]

        messages = []
        if isinstance(input_data, list):
            for item in input_data:
                if isinstance(item, dict):
                    role = item.get("role", "")
                    if role == "user":
                        content = item.get("content", "")
                        if isinstance(content, str):
                            messages.append({"role": "user", "content": content})
                        elif isinstance(content, list):
                            # Handle content parts
                            text_parts = []
                            for part in content:
                                if isinstance(part, dict) and part.get("type") == "input_text":
                                    text_parts.append(part.get("text", ""))
                                elif isinstance(part, str):
                                    text_parts.append(part)
                            if text_parts:
                                messages.append({"role": "user", "content": "\n".join(text_parts)})
                    elif role == "system":
                        content = item.get("content", "")
                        if isinstance(content, str):
                            messages.append({"role": "system", "content": content})
                    elif role == "assistant":
                        content = item.get("content", "")
                        if isinstance(content, str):
                            messages.append({"role": "assistant", "content": content})
                        elif isinstance(content, list):
                            text_parts = []
                            for part in content:
                                if isinstance(part, dict) and part.get("type") == "output_text":
                                    text_parts.append(part.get("text", ""))
                            if text_parts:
                                messages.append({"role": "assistant", "content": "\n".join(text_parts)})
        return messages

    async def create_response(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Handle non-streaming Responses API request."""
        model = request.get("model", "auto")
        input_data = request.get("input", [])
        instructions = request.get("instructions", "")

        messages = self._convert_input(input_data)
        if instructions:
            messages.insert(0, {"role": "system", "content": instructions})

        chat_request = {
            "model": model,
            "messages": messages,
            "stream": False,
        }

        result = await self.chat_adapter.chat_completion(chat_request)
        if "error" in result:
            return result

        # Convert chat completion to Responses format
        content_text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        return {
            "id": f"resp-{uuid.uuid4().hex[:24]}",
            "object": "response",
            "created_at": int(time.time()),
            "status": "completed",
            "model": model,
            "output": [{
                "type": "message",
                "id": f"msg-{uuid.uuid4().hex[:24]}",
                "status": "completed",
                "role": "assistant",
                "content": [{
                    "type": "output_text",
                    "text": content_text,
                }],
            }],
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            },
        }

    async def create_response_stream(self, request: Dict[str, Any]) -> AsyncGenerator[str, None]:
        """Handle streaming Responses API request."""
        model = request.get("model", "auto")
        input_data = request.get("input", [])
        instructions = request.get("instructions", "")

        messages = self._convert_input(input_data)
        if instructions:
            messages.insert(0, {"role": "system", "content": instructions})

        chat_request = {
            "model": model,
            "messages": messages,
            "stream": True,
        }

        response_id = f"resp-{uuid.uuid4().hex[:24]}"
        msg_id = f"msg-{uuid.uuid4().hex[:24]}"

        # Response created event
        created_event = {
            "type": "response.created",
            "response": {
                "id": response_id,
                "object": "response",
                "created_at": int(time.time()),
                "status": "in_progress",
                "model": model,
                "output": [],
            },
        }
        yield f"data: {json.dumps(created_event)}\n\n"

        # Content part start
        part_start = {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "type": "message",
                "id": msg_id,
                "status": "in_progress",
                "role": "assistant",
                "content": [],
            },
        }
        yield f"data: {json.dumps(part_start)}\n\n"

        text_part_id = f"text-{uuid.uuid4().hex[:24]}"
        content_start = {
            "type": "response.content_part.added",
            "part": {
                "type": "output_text",
                "text": "",
            },
        }
        yield f"data: {json.dumps(content_start)}\n\n"

        # Stream text deltas
        async for chunk in self.chat_adapter.chat_completion_stream(chat_request):
            # Parse the chunk to extract content
            if chunk.startswith("data: ") and chunk.strip() != "data: [DONE]":
                try:
                    data = json.loads(chunk[6:].strip())
                    delta = data.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        text_event = {
                            "type": "response.output_text.delta",
                            "delta": content,
                        }
                        yield f"data: {json.dumps(text_event)}\n\n"
                except (json.JSONDecodeError, IndexError):
                    pass

        # Content part done
        content_done = {
            "type": "response.content_part.done",
            "part": {
                "type": "output_text",
                "text": "",
            },
        }
        yield f"data: {json.dumps(content_done)}\n\n"

        # Output item done
        output_done = {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "type": "message",
                "id": msg_id,
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": ""}],
            },
        }
        yield f"data: {json.dumps(output_done)}\n\n"

        # Response completed
        completed_event = {
            "type": "response.completed",
            "response": {
                "id": response_id,
                "object": "response",
                "created_at": int(time.time()),
                "status": "completed",
                "model": model,
                "output": [{
                    "type": "message",
                    "id": msg_id,
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": ""}],
                }],
            },
        }
        yield f"data: {json.dumps(completed_event)}\n\n"
