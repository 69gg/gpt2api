"""
SSE (Server-Sent Events) stream parser for chatgpt.com.

Handles the text/event-stream format used by /backend-api/f/conversation
and /backend-api/conversation endpoints.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, Generator, Optional


# ChatGPT inline citation/navlist markers are wrapped in private-use-area
# control chars: "\ue200cite\ue202turn0finance0\ue201". They surface as stray
# "citeturn0finance0" text in API output, so strip them. We also strip the
# bare textual form in case the PUA delimiters are dropped upstream.
_CITATION_SPAN_RE = re.compile("\ue200.*?\ue201", re.S)
_CITATION_STRAY_RE = re.compile("[\ue200-\ue20f]")
_CITATION_TEXT_RE = re.compile(
    r"(?:cite|navlist|video)turn\d+[a-z]+\d+(?:turn\d+[a-z]+\d+)*"
)


def strip_citation_markers(text: str) -> str:
    """Remove ChatGPT citation/navlist markers (PUA-delimited or bare text)."""
    if not text:
        return text
    if "\ue200" in text:
        text = _CITATION_SPAN_RE.sub("", text)
        text = _CITATION_STRAY_RE.sub("", text)
    text = _CITATION_TEXT_RE.sub("", text)
    return text


class CitationStripper:
    """Stateful stripper for streamed deltas: drops PUA-delimited citation
    markers even when a marker is split across chunks, plus the bare text form."""

    # Maximum length of a bare-text citation token; keep this many trailing
    # characters across chunk boundaries so a token split between deltas can
    # still be matched and removed.
    _TAIL_BUF_LEN = 32

    def __init__(self) -> None:
        self._in_marker = False
        self._tail_buf = ""

    def feed(self, chunk: str) -> str:
        if not chunk:
            return chunk
        # Step 1: Strip PUA-delimited markers, preserving state across chunks.
        # The state only applies to the current chunk; any normal text before a
        # split marker is already in _tail_buf from the previous call.
        if self._in_marker or "\ue200" in chunk:
            out = []
            for ch in chunk:
                if self._in_marker:
                    if ch == "\ue201":
                        self._in_marker = False
                    continue
                if ch == "\ue200":
                    self._in_marker = True
                    continue
                if "\ue200" <= ch <= "\ue20f":
                    continue
                out.append(ch)
            chunk = "".join(out)
        # Step 2: Prepend any trailing tail from the previous chunk so a
        # bare-text citation token split across chunk boundaries can be matched
        # in full.  Downstream non-streaming code still calls
        # strip_citation_markers() on the aggregated content as a final cleanup.
        text = self._tail_buf + chunk
        self._tail_buf = ""
        text = _CITATION_TEXT_RE.sub("", text)
        # Retain a trailing window in case a citation token was cut off at the
        # end of this chunk.  Return everything before that window now.
        if len(text) > self._TAIL_BUF_LEN:
            self._tail_buf = text[-self._TAIL_BUF_LEN:]
            return text[:-self._TAIL_BUF_LEN]
        self._tail_buf = text
        return ""

    def flush(self) -> str:
        """Return any remaining buffered text and reset the stripper."""
        tail = self._tail_buf
        self._tail_buf = ""
        self._in_marker = False
        if not tail:
            return tail
        return _CITATION_TEXT_RE.sub("", tail)


def _term_finish(role: str, recipient: str, content_type: str) -> str:
    """Return "stop" only when the current message is the user-visible
    assistant answer. A bare {"p":"/message/status","v":"finished_successfully"}
    patch carries no role, and the web-search/tool message reports
    finished_successfully mid-stream too -- treating that as terminal would
    cut the stream off before the answer is generated."""
    if role not in ("assistant", ""):
        return ""
    if recipient not in ("all", ""):
        return ""
    if content_type not in ("text", "multimodal_text", ""):
        return ""
    return "stop"


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
    except GeneratorExit:
        return
    except Exception as e:
        yield SSEEvent(event="error", data="", error=str(e))
    finally:
        pass


def extract_chat_messages(events: Generator[SSEEvent, None, None]) -> Generator[ChatMessage, None, None]:
    """Extract ChatMessage objects from SSE events.

    Supports both old 'conversation' event type and new 'delta' event type
    with full-message updates and JSON Patch incremental updates.
    """
    accumulators: Dict[str, Dict[str, Any]] = {}
    for event in events:
        if event.error:
            yield ChatMessage(finish_reason="error", content=event.error)
            return

        # Support: conversation, delta, delta_encoding, or unnamed events
        if event.event not in ("conversation", "delta", "delta_encoding", ""):
            continue

        if not event.data or event.data == "[DONE]":
            if event.data == "[DONE]":
                yield ChatMessage(finish_reason="stop")
            continue

        try:
            data = json.loads(event.data)
        except json.JSONDecodeError:
            continue

        # Skip non-dict payloads (e.g. delta_encoding "v1")
        if not isinstance(data, dict):
            continue

        # --- Metadata events (message_marker, conversation_async_status, etc.) ---
        # These carry conversation_id / message_id needed for image generation polling
        evt_type = data.get("type", "")
        if evt_type in ("message_marker", "conversation_async_status",
                        "message_stream_complete", "input_message"):
            conv_id = data.get("conversation_id", "")
            msg_id = data.get("message_id", "") or (data.get("input_message") or {}).get("id", "")
            if conv_id or msg_id:
                yield ChatMessage(
                    conversation_id=conv_id,
                    message_id=msg_id,
                    role="assistant",
                    content="",
                )
            continue

        # --- Batch JSON Patch with no top-level path ---
        # Two forms, both used by the web-search / finance answer flow:
        #   {"o": "patch", "v": [{"p": "/message/content/parts/0", "o": "append", "v": "..."}]}
        #   {"v": [{"p": "/message/content/parts/0", "o": "append", "v": "..."}]}   (continuation)
        # Non-content list items (e.g. search_result_group) are filtered by the path check below.
        if "p" not in data and isinstance(data.get("v"), list):
            conv_id = data.get("conversation_id", "")
            msg_id = data.get("message_id", "")
            key = f"{conv_id}:{msg_id}"
            if key not in accumulators:
                accumulators[key] = {"text": "", "message_id": msg_id, "conversation_id": conv_id, "role": "", "recipient": "", "content_type": ""}
            for sub_patch in data["v"]:
                if not isinstance(sub_patch, dict):
                    continue
                sub_val = sub_patch.get("v")
                sub_path = sub_patch.get("p", "")
                if isinstance(sub_val, str) and "/content/parts/" in sub_path:
                    accumulators[key]["text"] += sub_val
                    yield ChatMessage(
                        message_id=msg_id, conversation_id=conv_id,
                        role="assistant", content=sub_val,
                    )
                elif sub_path == "/message/status" and sub_val == "finished_successfully":
                    acc = accumulators[key]
                    yield ChatMessage(
                        message_id=msg_id, conversation_id=conv_id,
                        role="assistant", content="", finish_reason=_term_finish(acc.get("role", ""), acc.get("recipient", ""), acc.get("content_type", "")),
                    )
            continue

        # --- JSON Patch incremental delta ---
        # e.g. {"p": "/message/content/parts/0", "o": "append", "v": "Hello"}
        # Also handles {"p": "", "o": "add", "v": {"message": ...}} from delta_encoding
        if "p" in data and "o" in data and "v" in data:
            patch_path = data.get("p", "")
            patch_op = data.get("o", "")
            patch_value = data["v"]

            # Full message add via delta_encoding: {"p": "", "o": "add", "v": {"message": ...}}
            if patch_path == "" and patch_op == "add" and isinstance(patch_value, dict):
                msg = patch_value.get("message", {})
                if msg:
                    conv_id = patch_value.get("conversation_id", "")
                    message_id = msg.get("id", "")
                    role = (msg.get("author") or {}).get("role", "")
                    content_parts = (msg.get("content") or {}).get("parts", [])
                    model = msg.get("model", "")
                    key = f"{conv_id}:{message_id}"
                    if key not in accumulators:
                        accumulators[key] = {"text": "", "message_id": message_id, "conversation_id": conv_id, "role": "", "recipient": "", "content_type": ""}
                    # Cache per-message context so later bare patches can tell
                    # the visible answer from tool/web-search messages.
                    accumulators[key]["role"] = role
                    accumulators[key]["recipient"] = msg.get("recipient", "")
                    accumulators[key]["content_type"] = (msg.get("content") or {}).get("content_type", "")
                    if role not in ("assistant", ""):
                        continue

                    # Build text content
                    text_parts = []
                    is_image = False
                    asset_pointer = ""
                    for part in content_parts:
                        if isinstance(part, str):
                            text_parts.append(part)
                        elif isinstance(part, dict):
                            if part.get("asset_pointer"):
                                asset_pointer = part["asset_pointer"]
                                is_image = True
                    content = "".join(text_parts)
                    if content:
                        accumulators[key]["text"] = content

                    # Finish reason
                    finish = ""
                    finish_details = (msg.get("metadata") or {}).get("finish_details", {})
                    if finish_details:
                        finish = finish_details.get("type", "")
                    if msg.get("status") == "finished_successfully" and not finish:
                        finish = "stop"

                    if content or is_image or finish:
                        yield ChatMessage(
                            message_id=message_id,
                            conversation_id=conv_id,
                            role=role,
                            content=content,
                            model=model,
                            finish_reason=finish,
                            is_image=is_image,
                            asset_pointer=asset_pointer,
                        )
                continue

            # Incremental text delta
            conv_id = data.get("conversation_id", "")
            msg_id = data.get("message_id", "")
            key = f"{conv_id}:{msg_id}"
            if key not in accumulators:
                accumulators[key] = {"text": "", "message_id": msg_id, "conversation_id": conv_id, "role": "", "recipient": "", "content_type": ""}
            # Track accumulation for full-message fallback, but yield only the delta
            patch_path = data.get("p", "")
            if isinstance(patch_value, str):
                if "parts/" in patch_path or patch_path == "":
                    # Text content delta
                    accumulators[key]["text"] += patch_value
                    yield ChatMessage(
                        message_id=msg_id,
                        conversation_id=conv_id,
                        role="assistant",
                        content=patch_value,
                    )
                elif patch_path == "/message/status" and patch_value == "finished_successfully":
                    acc = accumulators[key]
                    yield ChatMessage(
                        message_id=msg_id,
                        conversation_id=conv_id,
                        role="assistant",
                        content="",
                        finish_reason=_term_finish(acc.get("role", ""), acc.get("recipient", ""), acc.get("content_type", "")),
                    )
            elif isinstance(patch_value, list) and data.get("o") == "patch":
                # Batch patch: v is a list of patch operations
                for sub_patch in patch_value:
                    if isinstance(sub_patch, dict) and "p" in sub_patch and "o" in sub_patch and "v" in sub_patch:
                        sub_val = sub_patch["v"]
                        sub_path = sub_patch.get("p", "")
                        if isinstance(sub_val, str) and "parts/0" in sub_path:
                            accumulators[key]["text"] += sub_val
                            yield ChatMessage(
                                message_id=msg_id,
                                conversation_id=conv_id,
                                role="assistant",
                                content=sub_val,
                            )
                        elif sub_path == "/message/status" and sub_val == "finished_successfully":
                            acc = accumulators[key]
                            yield ChatMessage(
                                message_id=msg_id,
                                conversation_id=conv_id,
                                role="assistant",
                                content="",
                                finish_reason=_term_finish(acc.get("role", ""), acc.get("recipient", ""), acc.get("content_type", "")),
                            )
            continue

        # --- Simple string delta ---
        # e.g. {"v": " do this carefully, step"}
        if "v" in data and not isinstance(data.get("v"), dict):
            patch_value = data["v"]
            conv_id = data.get("conversation_id", "")
            msg_id = data.get("message_id", "")
            key = f"{conv_id}:{msg_id}"
            if key not in accumulators:
                accumulators[key] = {"text": "", "message_id": msg_id, "conversation_id": conv_id, "role": "", "recipient": "", "content_type": ""}
            if isinstance(patch_value, str):
                accumulators[key]["text"] += patch_value
                yield ChatMessage(
                    message_id=msg_id,
                    conversation_id=conv_id,
                    role="assistant",
                    content=patch_value,
                )
            continue

        # --- Full message update (nested under data["v"] or data["message"]) ---
        msg = data.get("message") or {}
        if not msg and isinstance(data.get("v"), dict):
            msg = data.get("v", {}).get("message", {})

        if not msg:
            # Check for error at top level
            if data.get("error"):
                yield ChatMessage(finish_reason="error", content=str(data["error"]))
                return
            continue

        message_id = msg.get("id") or data.get("message_id", "")
        conversation_id = data.get("conversation_id", "")
        role = (msg.get("author") or {}).get("role", "")
        # Cache per-message context in the accumulator so later bare patches
        # (e.g. batch JSON Patch finish signals) use this message's role.
        key = f"{conversation_id}:{message_id}"
        if key not in accumulators:
            accumulators[key] = {"text": "", "message_id": message_id, "conversation_id": conversation_id, "role": "", "recipient": "", "content_type": ""}
        accumulators[key]["role"] = role
        accumulators[key]["recipient"] = msg.get("recipient", "")
        accumulators[key]["content_type"] = (msg.get("content") or {}).get("content_type", "")
        # Skip non-assistant messages (user/system echoes in SSE stream)
        if role not in ("assistant", ""):
            continue
        content_parts = (msg.get("content") or {}).get("parts", [])
        model = msg.get("model", "") or data.get("model", "")

        # Detect replay: a full message update with status="finished_successfully"
        # and no prior delta content means this is a historical message being
        # replayed (e.g. previous assistant messages in multi-turn payload).
        # We must not yield its text content to avoid duplicating it.
        had_prior_delta = bool(accumulators[key]["text"])
        msg_status = msg.get("status", "")
        is_replay = not had_prior_delta and msg_status == "finished_successfully"

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

        # Build text content from parts
        text_parts = []
        for part in content_parts:
            if isinstance(part, str):
                text_parts.append(part)
            elif isinstance(part, dict):
                if part.get("asset_pointer"):
                    if not asset_pointer:
                        asset_pointer = part["asset_pointer"]
                    is_image = True
        content = "".join(text_parts)
        if content:
            accumulators[key]["text"] = content

        # Finish reason
        finish = data.get("finish_reason", "")
        if not finish and msg.get("finish_details"):
            finish = msg["finish_details"].get("type", "")

        if is_replay:
            # Replayed historical message: only yield metadata, not content
            if conversation_id or message_id:
                yield ChatMessage(
                    message_id=message_id,
                    conversation_id=conversation_id,
                    role=role,
                    content="",
                )
        else:
            # For new messages: don't re-yield text if already emitted via deltas
            emit_content = "" if had_prior_delta else accumulators[key]["text"]
            if emit_content or is_image or finish:
                yield ChatMessage(
                    message_id=message_id,
                    conversation_id=conversation_id,
                    role=role,
                    content=emit_content,
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


# ==================== Async versions ====================

async def async_parse_sse_stream(response) -> AsyncGenerator[SSEEvent, None]:
    """Async SSE stream parser for curl_cffi AsyncSession responses.

    Yields SSEEvent objects for each event in the stream.
    """
    event_type = ""
    data_buf = ""

    try:
        async for line in response.aiter_lines():
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
                if data_buf:
                    yield SSEEvent(event=event_type, data=data_buf)
                    event_type = ""
                    data_buf = ""
            else:
                if data_buf:
                    data_buf += "\n" + line
                else:
                    data_buf = line
    except Exception as e:
        yield SSEEvent(event="error", data="", error=str(e))


async def async_extract_chat_messages(events) -> AsyncGenerator[ChatMessage, None]:
    """Async version of extract_chat_messages — iterates over async SSEEvent generator."""
    accumulators: Dict[str, Dict[str, Any]] = {}
    async for event in events:
        if event.error:
            yield ChatMessage(finish_reason="error", content=event.error)
            return

        if event.event not in ("conversation", "delta", "delta_encoding", ""):
            continue

        if not event.data or event.data == "[DONE]":
            if event.data == "[DONE]":
                yield ChatMessage(finish_reason="stop")
            continue

        try:
            data = json.loads(event.data)
        except json.JSONDecodeError:
            continue

        if not isinstance(data, dict):
            continue

        evt_type = data.get("type", "")
        if evt_type in ("message_marker", "conversation_async_status",
                        "message_stream_complete", "input_message"):
            conv_id = data.get("conversation_id", "")
            msg_id = data.get("message_id", "") or (data.get("input_message") or {}).get("id", "")
            if conv_id or msg_id:
                yield ChatMessage(
                    conversation_id=conv_id,
                    message_id=msg_id,
                    role="assistant",
                    content="",
                )
            continue

        # Batch JSON Patch with no top-level path (web-search / finance answer flow).
        # Handles both {"o":"patch","v":[...]} and the {"v":[...]} continuation form.
        if "p" not in data and isinstance(data.get("v"), list):
            conv_id = data.get("conversation_id", "")
            msg_id = data.get("message_id", "")
            key = f"{conv_id}:{msg_id}"
            if key not in accumulators:
                accumulators[key] = {"text": "", "message_id": msg_id, "conversation_id": conv_id, "role": "", "recipient": "", "content_type": ""}
            for sub_patch in data["v"]:
                if not isinstance(sub_patch, dict):
                    continue
                sub_val = sub_patch.get("v")
                sub_path = sub_patch.get("p", "")
                if isinstance(sub_val, str) and "/content/parts/" in sub_path:
                    accumulators[key]["text"] += sub_val
                    yield ChatMessage(
                        message_id=msg_id, conversation_id=conv_id,
                        role="assistant", content=sub_val,
                    )
                elif sub_path == "/message/status" and sub_val == "finished_successfully":
                    acc = accumulators[key]
                    yield ChatMessage(
                        message_id=msg_id, conversation_id=conv_id,
                        role="assistant", content="", finish_reason=_term_finish(acc.get("role", ""), acc.get("recipient", ""), acc.get("content_type", "")),
                    )
            continue

        if "p" in data and "o" in data and "v" in data:
            patch_path = data.get("p", "")
            patch_op = data.get("o", "")
            patch_value = data["v"]

            if patch_path == "" and patch_op == "add" and isinstance(patch_value, dict):
                msg = patch_value.get("message", {})
                if msg:
                    conv_id = patch_value.get("conversation_id", "")
                    message_id = msg.get("id", "")
                    role = (msg.get("author") or {}).get("role", "")
                    content_parts = (msg.get("content") or {}).get("parts", [])
                    model = msg.get("model", "")
                    key = f"{conv_id}:{message_id}"
                    if key not in accumulators:
                        accumulators[key] = {"text": "", "message_id": message_id, "conversation_id": conv_id, "role": "", "recipient": "", "content_type": ""}
                    # Cache per-message context so later bare patches can tell
                    # the visible answer from tool/web-search messages.
                    accumulators[key]["role"] = role
                    accumulators[key]["recipient"] = msg.get("recipient", "")
                    accumulators[key]["content_type"] = (msg.get("content") or {}).get("content_type", "")
                    if role not in ("assistant", ""):
                        continue

                    text_parts = []
                    is_image = False
                    asset_pointer = ""
                    for part in content_parts:
                        if isinstance(part, str):
                            text_parts.append(part)
                        elif isinstance(part, dict):
                            if part.get("asset_pointer"):
                                asset_pointer = part["asset_pointer"]
                                is_image = True
                    content = "".join(text_parts)
                    if content:
                        accumulators[key]["text"] = content

                    finish = ""
                    finish_details = (msg.get("metadata") or {}).get("finish_details", {})
                    if finish_details:
                        finish = finish_details.get("type", "")
                    if msg.get("status") == "finished_successfully" and not finish:
                        finish = "stop"

                    if content or is_image or finish:
                        yield ChatMessage(
                            message_id=message_id,
                            conversation_id=conv_id,
                            role=role,
                            content=content,
                            model=model,
                            finish_reason=finish,
                            is_image=is_image,
                            asset_pointer=asset_pointer,
                        )
                continue

            conv_id = data.get("conversation_id", "")
            msg_id = data.get("message_id", "")
            key = f"{conv_id}:{msg_id}"
            if key not in accumulators:
                accumulators[key] = {"text": "", "message_id": msg_id, "conversation_id": conv_id, "role": "", "recipient": "", "content_type": ""}
            patch_path = data.get("p", "")
            if isinstance(patch_value, str):
                if "parts/" in patch_path or patch_path == "":
                    accumulators[key]["text"] += patch_value
                    yield ChatMessage(
                        message_id=msg_id,
                        conversation_id=conv_id,
                        role="assistant",
                        content=patch_value,
                    )
                elif patch_path == "/message/status" and patch_value == "finished_successfully":
                    acc = accumulators[key]
                    yield ChatMessage(
                        message_id=msg_id,
                        conversation_id=conv_id,
                        role="assistant",
                        content="",
                        finish_reason=_term_finish(acc.get("role", ""), acc.get("recipient", ""), acc.get("content_type", "")),
                    )
            elif isinstance(patch_value, list) and data.get("o") == "patch":
                for sub_patch in patch_value:
                    if isinstance(sub_patch, dict) and "p" in sub_patch and "o" in sub_patch and "v" in sub_patch:
                        sub_val = sub_patch["v"]
                        sub_path = sub_patch.get("p", "")
                        if isinstance(sub_val, str) and "parts/0" in sub_path:
                            accumulators[key]["text"] += sub_val
                            yield ChatMessage(
                                message_id=msg_id,
                                conversation_id=conv_id,
                                role="assistant",
                                content=sub_val,
                            )
                        elif sub_path == "/message/status" and sub_val == "finished_successfully":
                            acc = accumulators[key]
                            yield ChatMessage(
                                message_id=msg_id,
                                conversation_id=conv_id,
                                role="assistant",
                                content="",
                                finish_reason=_term_finish(acc.get("role", ""), acc.get("recipient", ""), acc.get("content_type", "")),
                            )
            continue

        if "v" in data and not isinstance(data.get("v"), dict):
            patch_value = data["v"]
            conv_id = data.get("conversation_id", "")
            msg_id = data.get("message_id", "")
            key = f"{conv_id}:{msg_id}"
            if key not in accumulators:
                accumulators[key] = {"text": "", "message_id": msg_id, "conversation_id": conv_id, "role": "", "recipient": "", "content_type": ""}
            if isinstance(patch_value, str):
                accumulators[key]["text"] += patch_value
                yield ChatMessage(
                    message_id=msg_id,
                    conversation_id=conv_id,
                    role="assistant",
                    content=patch_value,
                )
            continue

        msg = data.get("message") or {}
        if not msg and isinstance(data.get("v"), dict):
            msg = data.get("v", {}).get("message", {})

        if not msg:
            if data.get("error"):
                yield ChatMessage(finish_reason="error", content=str(data["error"]))
                return
            continue

        message_id = msg.get("id") or data.get("message_id", "")
        conversation_id = data.get("conversation_id", "")
        role = (msg.get("author") or {}).get("role", "")
        # Cache per-message context in the accumulator so later bare patches
        # (e.g. batch JSON Patch finish signals) use this message's role.
        key = f"{conversation_id}:{message_id}"
        if key not in accumulators:
            accumulators[key] = {"text": "", "message_id": message_id, "conversation_id": conversation_id, "role": "", "recipient": "", "content_type": ""}
        accumulators[key]["role"] = role
        accumulators[key]["recipient"] = msg.get("recipient", "")
        accumulators[key]["content_type"] = (msg.get("content") or {}).get("content_type", "")
        if role not in ("assistant", ""):
            continue
        content_parts = (msg.get("content") or {}).get("parts", [])
        model = msg.get("model", "") or data.get("model", "")

        had_prior_delta = bool(accumulators[key]["text"])
        msg_status = msg.get("status", "")
        is_replay = not had_prior_delta and msg_status == "finished_successfully"

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

        text_parts = []
        for part in content_parts:
            if isinstance(part, str):
                text_parts.append(part)
            elif isinstance(part, dict):
                if part.get("asset_pointer"):
                    if not asset_pointer:
                        asset_pointer = part["asset_pointer"]
                    is_image = True
        content = "".join(text_parts)
        if content:
            accumulators[key]["text"] = content

        finish = data.get("finish_reason", "")
        if not finish and msg.get("finish_details"):
            finish = msg["finish_details"].get("type", "")

        if is_replay:
            if conversation_id or message_id:
                yield ChatMessage(
                    message_id=message_id,
                    conversation_id=conversation_id,
                    role=role,
                    content="",
                )
        else:
            emit_content = "" if had_prior_delta else accumulators[key]["text"]
            if emit_content or is_image or finish:
                yield ChatMessage(
                    message_id=message_id,
                    conversation_id=conversation_id,
                    role=role,
                    content=emit_content,
                    model=model,
                    finish_reason=finish,
                    is_image=is_image,
                    image_url=image_url,
                    asset_pointer=asset_pointer,
                )


async def async_stream_text_from_response(response) -> AsyncGenerator[str, None]:
    """Async helper: yield text deltas from an SSE response."""
    async for msg in async_extract_chat_messages(async_parse_sse_stream(response)):
        if msg.finish_reason == "error":
            return
        if msg.content:
            yield msg.content
        if msg.finish_reason == "stop":
            return
