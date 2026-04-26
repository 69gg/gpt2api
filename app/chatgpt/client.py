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

import io
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Generator, List, Optional, Tuple

from curl_cffi import requests as curl_requests
from loguru import logger

from .sentinel import SentinelClient, POWConfig
from .retry import retry_call
from .sse import SSEEvent, ChatMessage, parse_sse_stream, extract_chat_messages, stream_text_from_response


BASE_URL = "https://chatgpt.com"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0"
)


@dataclass
class ChatOptions:
    """Options for a single chat request."""
    messages: List[Dict[str, Any]] = field(default_factory=list)
    model: str = "auto"
    conversation_id: str = ""
    parent_message_id: str = ""
    system_hints: List[str] = field(default_factory=list)
    sse_timeout: int = 120
    attachments: List[str] = field(default_factory=list)  # file_ids to attach


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


class ChatGPTClient:
    """ChatGPT Web Chat API client."""

    def __init__(self, access_token: str, device_id: str = "",
                 session_id: str = "", proxy: str = "",
                 user_agent: str = "", impersonate: str = "",
                 turnstile_solver_url: str = "",
                 pow_max_iter: int = 500000):
        self.access_token = access_token
        self.device_id = device_id or str(uuid.uuid4())
        self.session_id = session_id or str(uuid.uuid4())
        self.proxy = proxy
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self._proxies = {"http": proxy, "https": proxy} if proxy else None
        self._impersonate = impersonate or "chrome131"

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

    def prepare_fchat(self, chat_token: str, proof_token: str,
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
            for file_id in opts.attachments:
                parts.append({
                    "type": "image",
                    "asset_pointer": f"file-service://{file_id}",
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

        resp = retry_call(
            curl_requests.post,
            f"{BASE_URL}{path}", headers=headers, json=payload,
            proxies=self._proxies, impersonate=self._impersonate, timeout=30,
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

    def stream_fchat(self, chat_token: str, proof_token: str,
                     conduit_token: str, opts: ChatOptions) -> Generator[ChatMessage, None, None]:
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
                for file_id in opts.attachments:
                    parts.append({
                        "type": "image",
                        "asset_pointer": f"file-service://{file_id}",
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

        resp = retry_call(
            curl_requests.post,
            f"{BASE_URL}{path}", headers=headers, json=payload,
            proxies=self._proxies, impersonate=self._impersonate,
            timeout=opts.sse_timeout, stream=True,
            max_retries=2, delay=3.0, backoff=2.0, label="stream-fchat",
        )
        if resp.status_code >= 400:
            logger.error(f"f/conversation failed: {resp.status_code} {resp.text[:200]}")
            raise RuntimeError(f"f/conversation failed: {resp.status_code} {resp.text[:200]}")

        yield from extract_chat_messages(parse_sse_stream(resp))

    # ---------- Full chat flow ----------

    def chat(self, opts: ChatOptions) -> ChatResult:
        """Complete chat flow: bootstrap → chat-requirements → prepare → f/conversation."""
        logger.debug(f"chat(): model={opts.model}, msgs={len(opts.messages)}, conv_id={opts.conversation_id or '-'}")
        # 1. Bootstrap
        self.sentinel.bootstrap()

        # 2. Get chat requirements
        req_result = self.sentinel.get_chat_requirements()
        chat_token = req_result.token
        if not chat_token:
            raise RuntimeError("Failed to get chat_requirements token")

        # 3. Solve POW if needed
        proof_token = ""
        if req_result.proofofwork_required and req_result.proofofwork_seed:
            proof_token = self.sentinel.pow_config.solve_proof(
                req_result.proofofwork_seed, req_result.proofofwork_difficulty,
                self.sentinel.pow_max_iter,
            )

        # 4. Prepare
        conduit_token = self.prepare_fchat(chat_token, proof_token, opts)

        # 5. Stream f/conversation and collect result
        result = ChatResult()
        content_parts = []
        for msg in self.stream_fchat(chat_token, proof_token, conduit_token, opts):
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

        result.content = "".join(content_parts)
        return result

    def chat_stream(self, opts: ChatOptions) -> Generator[ChatMessage, None, None]:
        """Complete chat flow with streaming output."""
        logger.debug(f"chat_stream(): model={opts.model}, msgs={len(opts.messages)}, conv_id={opts.conversation_id or '-'}")
        self.sentinel.bootstrap()

        req_result = self.sentinel.get_chat_requirements()
        chat_token = req_result.token
        if not chat_token:
            raise RuntimeError("Failed to get chat_requirements token")

        proof_token = ""
        if req_result.proofofwork_required and req_result.proofofwork_seed:
            proof_token = self.sentinel.pow_config.solve_proof(
                req_result.proofofwork_seed, req_result.proofofwork_difficulty,
                self.sentinel.pow_max_iter,
            )

        conduit_token = self.prepare_fchat(chat_token, proof_token, opts)
        yield from self.stream_fchat(chat_token, proof_token, conduit_token, opts)

    # ---------- Legacy /backend-api/conversation ----------

    def stream_conversation(self, chat_token: str, proof_token: str,
                           opts: ChatOptions) -> Generator[ChatMessage, None, None]:
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

        resp = curl_requests.post(
            f"{BASE_URL}{path}", headers=headers, json=payload,
            proxies=self._proxies, impersonate=self._impersonate,
            timeout=opts.sse_timeout, stream=True,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"/conversation failed: {resp.status_code} {resp.text[:200]}")

        yield from extract_chat_messages(parse_sse_stream(resp))

    # ---------- File upload ----------

    def upload_file(self, file_bytes: bytes, filename: str = "image.png",
                    mime_type: str = "image/png") -> str:
        """Upload a file to ChatGPT file service, return file_id.

        POST /backend-api/files with multipart/form-data.
        curl_cffi uses 'multipart' param, not 'files'.
        """
        path = "/backend-api/files"
        headers = {
            **self._common_headers(path),
            "Accept": "*/*",
        }
        # Remove Content-Type so curl_cffi can set multipart boundary
        headers.pop("Content-Type", None)

        from curl_cffi.curl import CurlMime
        mime = CurlMime.from_list([{
            "name": "file",
            "content_type": mime_type,
            "filename": filename,
            "data": file_bytes,
        }])

        resp = retry_call(
            curl_requests.post,
            f"{BASE_URL}{path}", headers=headers,
            multipart=mime,
            proxies=self._proxies, impersonate=self._impersonate, timeout=60,
            max_retries=5, delay=1.0, backoff=2.0, label="upload-file",
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"File upload failed: {resp.status_code} {resp.text[:200]}")

        data = resp.json()
        file_id = data.get("file_id", "")
        if not file_id:
            file_id = data.get("id", "")
        if not file_id:
            raise RuntimeError(f"File upload returned no file_id: {data}")
        logger.debug(f"Upload OK: file_id={file_id}")
        return file_id
