"""
SSE (Server-Sent Events) stream parser for chatgpt.com.

Handles the text/event-stream format used by /backend-api/f/conversation
and /backend-api/conversation endpoints.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Generator, Optional


@dataclass
class SSEEvent:
    event: str = ""
    data: str = ""
    error: Optional[str] = None


@dataclass
class ChatMessage:
    """Parsed chat message from SSE stream."""
    message_id: str = ""
    conversation_id: str = ""
    role: str = ""
    content: str = ""
    model: str = ""
    finish_reason: str = ""
    is_image: bool = False
    image_url: str = ""
    asset_pointer: str = ""


def parse_sse_stream(response) -> Generator[SSEEvent, None, None]:
    """Parse SSE stream from a curl_cffi response object.

    Yields SSEEvent objects for each event in the stream.
    """
    event_type = ""
    data_buf = ""

    try:
        for line in response.iter_lines():
            if isinstance(line, bytes):
                line = line.decode("utf-8", errors="replace")

            line = line.rstrip("\r")

            if line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                chunk = line[5:]
                if data_buf:
                    data_buf += "\n" + chunk
                else:
                    data_buf = chunk
            elif line == "":
                # Empty line = end of event
                if data_buf:
                    yield SSEEvent(event=event_type, data=data_buf)
                    event_type = ""
                    data_buf = ""
            else:
                # Unknown line format, append to data
                if data_buf:
                    data_buf += "\n" + line
                else:
                    data_buf = line
    except Exception as e:
        yield SSEEvent(event="error", data="", error=str(e))
    finally:
        if data_buf:
            yield SSEEvent(event=event_type, data=data_buf)


def extract_chat_messages(events: Generator[SSEEvent, None, None]) -> Generator[ChatMessage, None, None]:
    """Extract ChatMessage objects from SSE events.

    Focuses on the 'conversation' event type which contains message deltas.
    """
    for event in events:
        if event.error:
            yield ChatMessage(finish_reason="error", content=event.error)
            return

        if event.event != "conversation" and event.event != "":
            continue

        if not event.data or event.data == "[DONE]":
            if event.data == "[DONE]":
                yield ChatMessage(finish_reason="stop")
            continue

        try:
            data = json.loads(event.data)
        except json.JSONDecodeError:
            continue

        msg = data.get("message") or data.get("v") or {}
        if not msg:
            # Check for error
            if data.get("error"):
                yield ChatMessage(finish_reason="error", content=str(data["error"]))
                return
            continue

        message_id = msg.get("id") or data.get("message_id", "")
        conversation_id = data.get("conversation_id", "")
        role = (msg.get("author") or {}).get("role", "")
        content_parts = (msg.get("content") or {}).get("parts", [])
        model = msg.get("model", "") or data.get("model", "")

        # Check for image
        is_image = False
        image_url = ""
        asset_pointer = ""
        content_type = (msg.get("content") or {}).get("content_type", "")
        if content_type == "image_asset_pointer":
            is_image = True
            for part in content_parts:
                if isinstance(part, dict):
                    asset_pointer = part.get("asset_pointer", "")
                    image_url = part.get("image_url", "") or part.get("url", "")
                elif isinstance(part, str):
                    if part.startswith("file-service://") or part.startswith("sediment://"):
                        asset_pointer = part

        # Build text content
        text_parts = []
        for part in content_parts:
            if isinstance(part, str):
                text_parts.append(part)
            elif isinstance(part, dict):
                # Image or other complex content
                if part.get("asset_pointer"):
                    if not asset_pointer:
                        asset_pointer = part["asset_pointer"]
                    is_image = True
        content = "".join(text_parts)

        # Finish reason
        finish = data.get("finish_reason", "")
        if not finish and msg.get("finish_details"):
            finish = msg["finish_details"].get("type", "")

        if content or is_image or finish:
            yield ChatMessage(
                message_id=message_id,
                conversation_id=conversation_id,
                role=role,
                content=content,
                model=model,
                finish_reason=finish,
                is_image=is_image,
                image_url=image_url,
                asset_pointer=asset_pointer,
            )


def stream_text_from_response(response) -> Generator[str, None, None]:
    """Simple helper: yield text deltas from an SSE response."""
    for msg in extract_chat_messages(parse_sse_stream(response)):
        if msg.finish_reason == "error":
            return
        if msg.content:
            yield msg.content
        if msg.finish_reason == "stop":
            return
