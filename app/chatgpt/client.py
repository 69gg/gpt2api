"""
ChatGPT Web Chat client — Python + curl_cffi rewrite of the Go client.

Handles:
- Bootstrap (GET / for CF cookies)
- Chat requirements (sentinel + POW)
- f/conversation/prepare (conduit token)
- f/conversation (SSE text chat)
- conversation endpoint (legacy SSE)
"""
from __future__ import annotations

import asyncio
import io
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, Generator, List, Optional, Tuple

from curl_cffi.requests import AsyncSession
from loguru import logger

from .sentinel import SentinelClient, POWConfig
from .retry import async_retry_call
from .sse import (
    SSEEvent, ChatMessage,
    async_parse_sse_stream, async_extract_chat_messages, async_stream_text_from_response,
    strip_citation_markers, CitationStripper,
)


BASE_URL = "https://chatgpt.com"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0"
)


def _image_dimensions(data: bytes) -> Tuple[int, int]:
    """Extract width, height from PNG or JPEG bytes. Returns (0, 0) if unknown."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 24:
        import struct
        w, h = struct.unpack(">II", data[16:24])
        return w, h
    if data[:2] == b"\xff\xd8" and len(data) > 5:
        import struct
        off = 2
        while off < len(data) - 1:
            if data[off] != 0xFF:
                break
            marker = data[off + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                if off + 9 <= len(data):
                    h, w = struct.unpack(">HH", data[off + 5:off + 9])
                    return w, h
            if marker in (0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9):
                off += 2
            else:
                if off + 4 > len(data):
                    break
                seg_len = struct.unpack(">H", data[off + 2:off + 4])[0]
                off += 2 + seg_len
    return 0, 0


@dataclass
class ChatOptions:
    """Options for a single chat request."""
    messages: List[Dict[str, Any]] = field(default_factory=list)
    model: str = "auto"
    conversation_id: str = ""
    parent_message_id: str = ""
    system_hints: List[str] = field(default_factory=list)
    sse_timeout: int = 120
    attachments: List[Any] = field(default_factory=list)  # file_ids or dicts {file_id, size_bytes, width, height}


@dataclass
class ChatResult:
    """Result of a chat request."""
    content: str = ""
    conversation_id: str = ""
    message_id: str = ""
    model: str = ""
    finish_reason: str = ""
    is_image: bool = False
    image_url: str = ""
    asset_pointer: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0


def estimate_tokens(text: str) -> int:
    """Rough token count estimation.

    Heuristic: CJK chars ~1.5 tokens each, ASCII words ~1 token each,
    punctuation/whitespace ~0.5 tokens each. Simplified to:
    - Count CJK characters (each ≈ 1-2 tokens)
    - Split remaining by whitespace, each word ≈ 1-1.5 tokens
    - Add a small overhead for formatting
    """
    if not text:
        return 0
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3400' <= c <= '\u4dbf')
    # For ASCII-heavy text, use len/4 as baseline; for CJK, use char count * 1.3
    ascii_chars = len(text) - cjk
    return max(1, int(cjk * 1.3 + ascii_chars / 3.5))


def estimate_messages_tokens(messages: List[Dict[str, Any]]) -> int:
    """Estimate total tokens for a list of chat messages."""
    total = 0
    for msg in messages:
        # Each message has ~4 tokens overhead (role, formatting)
        total += 4
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += estimate_tokens(part.get("text", ""))
    return total


class ChatGPTClient:
    """ChatGPT Web Chat API client (async)."""

    def __init__(self, access_token: str, device_id: str = "",
                 session_id: str = "", proxy: str = "",
                 user_agent: str = "", impersonate: str = "",
                 turnstile_solver_url: str = "",
                 pow_max_iter: int = 500000,
                 session: Optional[AsyncSession] = None):
        self.access_token = access_token
        self.device_id = device_id or str(uuid.uuid4())
        self.session_id = session_id or str(uuid.uuid4())
        self.proxy = proxy
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self._proxies = {"http": proxy, "https": proxy} if proxy else None
        self._impersonate = impersonate or "chrome131"
        self._session = session  # shared AsyncSession, created lazily if None

        self.sentinel = SentinelClient(
            access_token=access_token,
            device_id=self.device_id,
            session_id=self.session_id,
            proxy=proxy,
            user_agent=self.user_agent,
            impersonate=self._impersonate,
            turnstile_solver_url=turnstile_solver_url,
            pow_max_iter=pow_max_iter,
        )

    def _get_session(self) -> AsyncSession:
        """Get or create the AsyncSession (must be called within a running event loop)."""
        if self._session is None:
            self._session = AsyncSession(
                impersonate=self._impersonate,
                proxies=self._proxies,
                timeout=30,
                max_clients=100,
            )
        return self._session

    def _common_headers(self, path: str = "") -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "User-Agent": self.user_agent,
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
            "Sec-Ch-Ua": '"Microsoft Edge";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
            "Sec-Ch-Ua-Arch": '"x86"',
            "Sec-Ch-Ua-Bitness": '"64"',
            "Sec-Ch-Ua-Full-Version": '"143.0.3650.96"',
            "Sec-Ch-Ua-Full-Version-List": '"Microsoft Edge";v="143.0.3650.96", "Chromium";v="143.0.7499.147", "Not A(Brand";v="24.0.0.0"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Model": '""',
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Ch-Ua-Platform-Version": '"19.0.0"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Priority": "u=1, i",
            "Oai-Device-Id": self.device_id,
            "Oai-Session-Id": self.session_id,
            "Oai-Language": "zh-CN",
            "Oai-Client-Version": "prod-be885abbfcfe7b1f511e88b3003d9ee44757fbad",
            "Oai-Client-Build-Number": "5955942",
            "X-Openai-Target-Path": path,
            "X-Openai-Target-Route": path,
        }

    # ---------- f/conversation/prepare ----------

    async def prepare_fchat(self, chat_token: str, proof_token: str,
                      opts: ChatOptions) -> str:
        """POST /backend-api/f/conversation/prepare → conduit_token."""
        path = "/backend-api/f/conversation/prepare"
        parent_msg_id = opts.parent_message_id or str(uuid.uuid4())
        user_content = ""
        for m in reversed(opts.messages):
            if m.get("role") == "user":
                user_content = m.get("content", "")
                break

        # Build partial_query content (multimodal if attachments present)
        if opts.attachments:
            parts = [{"type": "text", "text": user_content}]
            for att in opts.attachments:
                if isinstance(att, dict):
                    fid = att.get("file_id", att.get("id", ""))
                    parts.append({
                        "content_type": "image_asset_pointer",
                        "asset_pointer": f"file-service://{fid}",
                        "size_bytes": att.get("size_bytes", 0),
                        "width": att.get("width", 0),
                        "height": att.get("height", 0),
                    })
                else:
                    parts.append({
                        "content_type": "image_asset_pointer",
                        "asset_pointer": f"file-service://{att}",
                        "size_bytes": 0,
                        "width": 0,
                        "height": 0,
                    })
            partial_content = {"content_type": "multimodal_text", "parts": parts}
        else:
            partial_content = {"content_type": "text", "parts": [user_content]}

        payload = {
            "action": "next",
            "parent_message_id": parent_msg_id,
            "model": opts.model,
            "client_prepare_state": "success",
            "timezone_offset_min": -480,
            "timezone": "Asia/Shanghai",
            "conversation_mode": {"kind": "primary_assistant"},
            "system_hints": opts.system_hints,
            "partial_query": {
                "id": str(uuid.uuid4()),
                "author": {"role": "user"},
                "content": partial_content,
            },
            "supports_buffering": True,
            "supported_encodings": ["v1"],
            "client_contextual_info": {"app_name": "chatgpt.com"},
        }
        if opts.conversation_id:
            payload["conversation_id"] = opts.conversation_id

        headers = {
            **self._common_headers(path),
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Openai-Sentinel-Chat-Requirements-Token": chat_token,
        }
        if proof_token:
            headers["Openai-Sentinel-Proof-Token"] = proof_token

        session = self._get_session()
        resp = await async_retry_call(
            session.post,
            f"{BASE_URL}{path}", headers=headers, json=payload,
            timeout=30,
            max_retries=3, delay=2.0, backoff=2.0, label="prepare",
        )
        if resp.status_code >= 400:
            logger.error(f"f/conversation/prepare failed: {resp.status_code} {resp.text[:200]}")
            raise RuntimeError(f"f/conversation/prepare failed: {resp.status_code} {resp.text[:200]}")

        data = resp.json()
        conduit_token = data.get("conduit_token", "")
        logger.debug(f"Prepare OK: conduit_token={bool(conduit_token)}")
        return conduit_token

    # ---------- f/conversation (SSE) ----------

    async def stream_fchat(self, chat_token: str, proof_token: str,
                     conduit_token: str, opts: ChatOptions) -> AsyncGenerator[ChatMessage, None]:
        """POST /backend-api/f/conversation → SSE stream of ChatMessage."""
        path = "/backend-api/f/conversation"
        parent_msg_id = opts.parent_message_id or str(uuid.uuid4())

        # Build messages payload (align with HAR capture for text pathway)
        # If attachments are present, the last user message becomes multimodal.
        msgs = []
        attachment_idx = 0
        for i, m in enumerate(opts.messages):
            role = m.get("role", "user")
            content = m.get("content", "")
            is_last_user = (i == len(opts.messages) - 1) and role == "user"
            has_attachments = is_last_user and opts.attachments

            if has_attachments:
                # Multimodal message with text + image attachments
                parts = [{"type": "text", "text": content}]
                for att in opts.attachments:
                    if isinstance(att, dict):
                        fid = att.get("file_id", att.get("id", ""))
                        parts.append({
                            "content_type": "image_asset_pointer",
                            "asset_pointer": f"file-service://{fid}",
                            "size_bytes": att.get("size_bytes", 0),
                            "width": att.get("width", 0),
                            "height": att.get("height", 0),
                        })
                    else:
                        parts.append({
                            "content_type": "image_asset_pointer",
                            "asset_pointer": f"file-service://{att}",
                            "size_bytes": 0,
                            "width": 0,
                            "height": 0,
                        })
                msg_content = {
                    "content_type": "multimodal_text",
                    "parts": parts,
                }
            else:
                msg_content = {
                    "content_type": "text",
                    "parts": [content],
                }

            msgs.append({
                "id": str(uuid.uuid4()),
                "author": {"role": role},
                "create_time": time.time(),
                "content": msg_content,
                "metadata": {
                    "developer_mode_connector_ids": [],
                    "selected_sources": [],
                    "selected_github_repos": [],
                    "selected_all_github_repos": False,
                    "serialization_metadata": {"custom_symbol_offsets": []},
                },
            })

        payload = {
            "action": "next",
            "messages": msgs,
            "parent_message_id": parent_msg_id,
            "model": opts.model,
            "client_prepare_state": "sent",
            "timezone_offset_min": -480,
            "timezone": "Asia/Shanghai",
            "conversation_mode": {"kind": "primary_assistant"},
            "enable_message_followups": True,
            "system_hints": opts.system_hints,
            "supports_buffering": True,
            "supported_encodings": ["v1"],
            "client_contextual_info": {
                "is_dark_mode": False,
                "time_since_loaded": 1200,
                "page_height": 1072,
                "page_width": 1724,
                "pixel_ratio": 1.2,
                "screen_height": 1440,
                "screen_width": 2560,
                "app_name": "chatgpt.com",
            },
            "paragen_cot_summary_display_override": "allow",
            "force_parallel_switch": "auto",
        }
        if opts.conversation_id:
            payload["conversation_id"] = opts.conversation_id

        headers = {
            **self._common_headers(path),
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "X-Oai-Turn-Trace-Id": str(uuid.uuid4()),
            "Openai-Sentinel-Chat-Requirements-Token": chat_token,
        }
        if proof_token:
            headers["Openai-Sentinel-Proof-Token"] = proof_token
        if conduit_token:
            headers["Openai-Sentinel-Conduit-Api-Token"] = conduit_token

        session = self._get_session()
        resp = await async_retry_call(
            session.post,
            f"{BASE_URL}{path}", headers=headers, json=payload,
            timeout=opts.sse_timeout, stream=True,
            max_retries=2, delay=3.0, backoff=2.0, label="stream-fchat",
        )
        if resp.status_code >= 400:
            logger.error(f"f/conversation failed: {resp.status_code} {resp.text[:200]}")
            raise RuntimeError(f"f/conversation failed: {resp.status_code} {resp.text[:200]}")

        async for msg in async_extract_chat_messages(async_parse_sse_stream(resp)):
            yield msg

    # ---------- Full chat flow ----------

    async def chat(self, opts: ChatOptions) -> ChatResult:
        """Complete chat flow: bootstrap → chat-requirements → prepare → f/conversation."""
        logger.debug(f"chat(): model={opts.model}, msgs={len(opts.messages)}, conv_id={opts.conversation_id or '-'}")
        # 1. Bootstrap
        await self.sentinel.bootstrap()

        # 2. Get chat requirements
        req_result = await self.sentinel.get_chat_requirements()
        chat_token = req_result.token
        if not chat_token:
            raise RuntimeError("Failed to get chat_requirements token")

        # 3. Solve POW if needed (CPU-bound, offload to thread)
        proof_token = ""
        if req_result.proofofwork_required and req_result.proofofwork_seed:
            proof_token = await asyncio.to_thread(
                self.sentinel.pow_config.solve_proof,
                req_result.proofofwork_seed, req_result.proofofwork_difficulty,
                self.sentinel.pow_max_iter,
            )

        # 4. Prepare
        conduit_token = await self.prepare_fchat(chat_token, proof_token, opts)

        # 5. Stream f/conversation and collect result
        result = ChatResult()
        content_parts = []
        async for msg in self.stream_fchat(chat_token, proof_token, conduit_token, opts):
            if msg.content:
                content_parts.append(msg.content)
            if msg.conversation_id:
                result.conversation_id = msg.conversation_id
            if msg.message_id:
                result.message_id = msg.message_id
            if msg.model:
                result.model = msg.model
            if msg.is_image:
                result.is_image = True
                result.image_url = msg.image_url
                result.asset_pointer = msg.asset_pointer
            if msg.finish_reason:
                result.finish_reason = msg.finish_reason

        result.content = strip_citation_markers("".join(content_parts))

        # Estimate token usage
        result.prompt_tokens = estimate_messages_tokens(opts.messages)
        result.completion_tokens = estimate_tokens(result.content)
        return result

    async def chat_stream(self, opts: ChatOptions) -> AsyncGenerator[ChatMessage, None]:
        """Complete chat flow with streaming output."""
        logger.debug(f"chat_stream(): model={opts.model}, msgs={len(opts.messages)}, conv_id={opts.conversation_id or '-'}")
        await self.sentinel.bootstrap()

        req_result = await self.sentinel.get_chat_requirements()
        chat_token = req_result.token
        if not chat_token:
            raise RuntimeError("Failed to get chat_requirements token")

        proof_token = ""
        if req_result.proofofwork_required and req_result.proofofwork_seed:
            proof_token = await asyncio.to_thread(
                self.sentinel.pow_config.solve_proof,
                req_result.proofofwork_seed, req_result.proofofwork_difficulty,
                self.sentinel.pow_max_iter,
            )

        conduit_token = await self.prepare_fchat(chat_token, proof_token, opts)
        stripper = CitationStripper()
        async for msg in self.stream_fchat(chat_token, proof_token, conduit_token, opts):
            cleaned = ""
            if msg.content and not msg.is_image:
                cleaned = stripper.feed(msg.content)
            if msg.finish_reason:
                # Flush any trailing buffer that may have held a split
                # citation-token prefix once the stream terminates.
                tail = stripper.flush()
                if tail:
                    cleaned += tail
            if not cleaned and not msg.finish_reason:
                # Entire delta was citation markup; nothing to emit
                continue
            if cleaned and not msg.is_image:
                msg.content = cleaned
            yield msg

    # ---------- Legacy /backend-api/conversation ----------

    async def stream_conversation(self, chat_token: str, proof_token: str,
                           opts: ChatOptions) -> AsyncGenerator[ChatMessage, None]:
        """POST /backend-api/conversation → SSE stream (legacy endpoint)."""
        path = "/backend-api/conversation"
        parent_msg_id = opts.parent_message_id or str(uuid.uuid4())

        msgs = []
        for m in opts.messages:
            msgs.append({
                "id": str(uuid.uuid4()),
                "author": {"role": m["role"]},
                "content": {"content_type": "text", "parts": [m["content"]]},
            })

        payload = {
            "action": "next",
            "messages": msgs,
            "parent_message_id": parent_msg_id,
            "model": opts.model,
            "timezone_offset_min": -480,
            "suggestions": [],
            "history_and_training_disabled": False,
            "conversation_mode": {"kind": "primary_assistant"},
            "force_paragen": False,
            "force_paragen_model_slug": "",
            "force_nulligen": False,
            "force_rate_limit": False,
            "reset_rate_limits": False,
            "websocket_request_id": str(uuid.uuid4()),
            "system_hints": opts.system_hints,
            "supported_encodings": ["v1"],
            "client_contextual_info": {
                "is_dark_mode": False,
                "time_since_loaded": 1200,
                "page_height": 1072,
                "page_width": 1724,
                "pixel_ratio": 1.2,
                "screen_height": 1440,
                "screen_width": 2560,
                "app_name": "chatgpt.com",
            },
        }
        if opts.conversation_id:
            payload["conversation_id"] = opts.conversation_id

        headers = {
            **self._common_headers(path),
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Openai-Sentinel-Chat-Requirements-Token": chat_token,
        }
        if proof_token:
            headers["Openai-Sentinel-Proof-Token"] = proof_token

        session = self._get_session()
        resp = await session.post(
            f"{BASE_URL}{path}", headers=headers, json=payload,
            timeout=opts.sse_timeout, stream=True,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"/conversation failed: {resp.status_code} {resp.text[:200]}")

        async for msg in async_extract_chat_messages(async_parse_sse_stream(resp)):
            yield msg

    # ---------- File upload ----------

    async def upload_file(self, file_bytes: bytes, filename: str = "image.png",
                    mime_type: str = "image/png") -> Dict[str, Any]:
        """Upload a file to ChatGPT file service, return file metadata dict.

        Three-step protocol:
        1. POST /backend-api/files JSON body → get presigned upload_url + file_id
        2. PUT upload_url with file bytes + x-ms-blob-type: BlockBlob → Azure Blob Storage
        3. POST /backend-api/files/{file_id}/uploaded → confirm upload, file becomes "ready"

        Returns dict with file_id, size_bytes, width, height for use as attachment.
        """
        path = "/backend-api/files"
        session = self._get_session()

        # Step 1: Request presigned upload URL
        resp = await async_retry_call(
            session.post,
            f"{BASE_URL}{path}",
            headers={
                **self._common_headers(path),
                "Accept": "*/*",
                "Content-Type": "application/json",
            },
            json={"file_name": filename, "use_case": "multimodal"},
            timeout=30,
            max_retries=3, delay=1.0, backoff=2.0, label="upload-file-init",
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"File upload init failed: {resp.status_code} {resp.text[:200]}")

        data = resp.json()
        upload_url = data.get("upload_url", "")
        file_id = data.get("file_id", "")
        if not file_id:
            file_id = data.get("id", "")
        if not file_id or not upload_url:
            raise RuntimeError(f"File upload init returned no file_id/upload_url: {data}")

        # Step 2: Upload file bytes to Azure Blob Storage presigned URL
        put_resp = await async_retry_call(
            session.put,
            upload_url,
            headers={
                "Content-Type": mime_type,
                "x-ms-blob-type": "BlockBlob",
                "Content-Length": str(len(file_bytes)),
            },
            data=file_bytes,
            timeout=60,
            max_retries=3, delay=1.0, backoff=2.0, label="upload-file-put",
        )
        if put_resp.status_code >= 400:
            raise RuntimeError(f"File upload PUT failed: {put_resp.status_code} {put_resp.text[:200]}")

        # Step 3: Confirm upload completion
        confirm_path = f"{path}/{file_id}/uploaded"
        confirm_resp = await async_retry_call(
            session.post,
            f"{BASE_URL}{confirm_path}",
            headers={
                **self._common_headers(confirm_path),
                "Accept": "*/*",
                "Content-Type": "application/json",
            },
            json={},
            timeout=30,
            max_retries=2, delay=1.0, backoff=2.0, label="upload-file-confirm",
        )
        if confirm_resp.status_code >= 400:
            raise RuntimeError(f"File upload confirm failed: {confirm_resp.status_code} {confirm_resp.text[:200]}")

        # Extract metadata from confirm response
        confirm_data = confirm_resp.json() if confirm_resp.status_code == 200 else {}

        # Auto-detect image dimensions for image uploads
        width = confirm_data.get("width", 0)
        height = confirm_data.get("height", 0)
        if not width or not height:
            width, height = _image_dimensions(file_bytes)

        result = {
            "file_id": file_id,
            "size_bytes": len(file_bytes),
            "width": width or 0,
            "height": height or 0,
        }

        logger.debug(f"Upload OK: file_id={file_id}")
        return result

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session:
            await self._session.close()
            self._session = None
        await self.sentinel.close()
