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
from app.chatgpt.tool_call import (
    build_tool_prompt, format_tool_history, parse_tool_calls, ToolCallStreamParser,
)
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
    return "image" in model.lower() or "dall" in model.lower()


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
        image_url, _ = img_client._poll_for_image(conv_id, msg_id, "", max_wait=max_wait)

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
            image_url, _ = img_client._poll_for_image(conv_id, msg_id, asset_pointer, max_wait=120)

        if image_url and self.deployment_url:
            image_url = _make_proxy_url(image_url, self.deployment_url)

        return image_url

    async def _image_via_chat(self, request: Dict[str, Any],
                              token: "TokenInfo") -> Dict[str, Any]:
        """Handle image model via ImageClient.generate (avoids 413 from chat payload)."""
        from app.chatgpt.image import ImageClient
        from app.adapters.openai_image import _make_proxy_url

        model = request.get("model", "auto")
        upstream_model = _map_model(model)
        messages = request.get("messages", [])

        # Extract last user message as image prompt
        prompt = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    prompt = content
                elif isinstance(content, list):
                    # multimodal: extract text parts
                    prompt = " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
                break

        if not prompt:
            return {"error": "invalid_request", "message": "No user message found for image generation"}

        prompt = f"根据以下要求生成图片：{prompt}"

        client = self._create_client(token)
        img_client = ImageClient(client)

        try:
            logger.info(f"ImageChat [{token.email}]: model={model} → upstream={upstream_model}, prompt={prompt[:60]}")
            result = img_client.generate(prompt, model=upstream_model)
            logger.info(f"ImageChat [{token.email}]: status={result.status}, url={bool(result.image_url)}, asset={result.asset_pointer[:50] if result.asset_pointer else '-'}")

            if result.status != "success" or not result.image_url:
                token.record_failure(FailReason.UNKNOWN)
                token.save()
                return {"error": "generation_failed", "message": "Image generation failed"}

            image_url = result.image_url
            if self.deployment_url:
                image_url = _make_proxy_url(image_url, self.deployment_url)

            content = f"![image]({image_url})"
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
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
        except Exception as e:
            error_str = str(e)
            reason = self.token_manager.classify_error(0, error_str)
            token.record_failure(reason)
            token.save()
            logger.error(f"ImageChat [{token.email}]: FAILED reason={reason} err={e}")
            return {"error": "upstream_error", "message": error_str}

    def _image_via_chat_stream(self, request: Dict[str, Any],
                               token: "TokenInfo") -> Generator[str, None, None]:
        """Handle image model streaming via ImageClient.generate."""
        from app.chatgpt.image import ImageClient
        from app.adapters.openai_image import _make_proxy_url

        model = request.get("model", "auto")
        upstream_model = _map_model(model)
        messages = request.get("messages", [])
        chat_id = f"chatcmpl-{uuid.uuid4().hex[:29]}"
        created = int(time.time())

        # Extract last user message as image prompt
        prompt = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    prompt = content
                elif isinstance(content, list):
                    prompt = " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
                break

        if not prompt:
            return

        prompt = f"根据以下要求生成图片：{prompt}"

        client = self._create_client(token)
        img_client = ImageClient(client)

        try:
            # Yield role delta
            role_chunk = {
                "id": chat_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(role_chunk)}\n\n"

            logger.info(f"ImageStream [{token.email}]: model={model} → upstream={upstream_model}, prompt={prompt[:60]}")
            result = img_client.generate(prompt, model=upstream_model)
            logger.info(f"ImageStream [{token.email}]: status={result.status}, url={bool(result.image_url)}")

            if result.status == "success" and result.image_url:
                image_url = result.image_url
                if self.deployment_url:
                    image_url = _make_proxy_url(image_url, self.deployment_url)

                content_chunk = {
                    "id": chat_id, "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{"index": 0, "delta": {"content": f"![image]({image_url})"}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(content_chunk)}\n\n"

                token.record_success()
            else:
                token.record_failure(FailReason.UNKNOWN)

            token.save()

            finish_chunk = {
                "id": chat_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(finish_chunk)}\n\n"
        except Exception as e:
            error_str = str(e)
            reason = self.token_manager.classify_error(0, error_str)
            token.record_failure(reason)
            token.save()
            logger.error(f"ImageStream [{token.email}]: FAILED reason={reason} err={e}")

        yield "data: [DONE]\n\n"

    def _build_messages(self, messages: List[Dict[str, Any]],
                        tools: List[Dict[str, Any]] = None,
                        tool_choice: Any = None,
                        parallel_tool_calls: bool = True) -> List[Dict[str, Any]]:
        """Convert OpenAI messages to ChatGPT format.

        If tools are provided, tool-related messages are converted to text form
        and a tool-calling contract prompt is prepended.
        """
        # Convert tool history to text form if tools are present
        if tools:
            messages = format_tool_history(messages)

        result = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                result.append({"role": "user", "content": f"[System Instructions]\n{content}"})
                result.append({"role": "assistant", "content": "Understood."})
            elif role in ("user", "assistant"):
                result.append({"role": role, "content": content})
            elif role == "tool":
                # Already handled by format_tool_history above
                pass

        # Prepend tool prompt if tools are defined
        if tools:
            tool_prompt = build_tool_prompt(tools, tool_choice, parallel_tool_calls)
            if tool_prompt:
                result.insert(0, {"role": "user", "content": tool_prompt})
                result.insert(1, {"role": "assistant", "content": "Understood. I will follow the tool calling contract."})

        return result

    async def chat_completion(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Handle non-streaming chat completion request."""
        token = self.token_manager.get_available()
        if not token:
            return {"error": "no_available_token", "message": "No available tokens in pool"}

        model = request.get("model", "auto")

        # Delegate image models to ImageClient to avoid 413 from multi-msg payloads
        if _is_image_model(model) and not request.get("tools"):
            return await self._image_via_chat(request, token)

        client = self._create_client(token)
        upstream_model = _map_model(model)
        tools = request.get("tools")
        tool_choice = request.get("tool_choice")
        parallel_tool_calls = request.get("parallel_tool_calls", True)
        messages = self._build_messages(
            request.get("messages", []),
            tools=tools, tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
        )

        if not messages:
            return {"error": "invalid_request", "message": "Messages cannot be empty"}

        # Detect image generation model and inject picture_v2 hint + prompt
        is_image = _is_image_model(model)
        system_hints = ["picture_v2"] if is_image else []
        if is_image and messages:
            last = messages[-1]
            if last.get("role") == "user":
                last["content"] = f"根据以下要求生成图片：{last['content']}"

        opts = ChatOptions(
            messages=messages,
            model=upstream_model,
            sse_timeout=self.sse_timeout,
            system_hints=system_hints,
        )

        try:
            logger.info(f"Chat [{token.email}]: model={model} → upstream={upstream_model}, msgs={len(messages)}, tools={bool(tools)}, image={is_image}")
            result = client.chat(opts)
            logger.info(f"Chat [{token.email}]: finished, content_len={len(result.content)}, finish={result.finish_reason}, is_image={result.is_image}")

            content = result.content
            # If image was generated, resolve the image URL and append it.
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
                content = f"![image]({image_url})"
            elif result.is_image:
                logger.warning(f"Chat [{token.email}]: image detected but URL resolution failed")

            # Parse tool calls if tools were provided
            finish_reason = result.finish_reason or "stop"
            tool_calls_result = None
            if tools and tool_choice != "none":
                text_content, tool_calls_list = parse_tool_calls(content, tools)
                if tool_calls_list:
                    tool_calls_result = tool_calls_list
                    content = text_content  # May be None
                    finish_reason = "tool_calls"
                    logger.info(f"Chat [{token.email}]: parsed {len(tool_calls_list)} tool call(s)")

            token.record_success()
            token.save()

            message_obj = {"role": "assistant", "content": content}
            if tool_calls_result:
                message_obj["tool_calls"] = tool_calls_result

            return {
                "id": f"chatcmpl-{uuid.uuid4().hex[:29]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": message_obj,
                    "finish_reason": finish_reason,
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

        model = request.get("model", "auto")

        # Delegate image models to ImageClient to avoid 413
        if _is_image_model(model) and not request.get("tools"):
            yield from self._image_via_chat_stream(request, token)
            return

        client = self._create_client(token)
        upstream_model = _map_model(model)
        tools = request.get("tools")
        tool_choice = request.get("tool_choice")
        parallel_tool_calls = request.get("parallel_tool_calls", True)
        messages = self._build_messages(
            request.get("messages", []),
            tools=tools, tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
        )
        chat_id = f"chatcmpl-{uuid.uuid4().hex[:29]}"
        created = int(time.time())
        logger.info(f"Stream [{token.email}]: model={model} → upstream={upstream_model}, msgs={len(messages)}, tools={bool(tools)}")

        if not messages:
            return

        # Detect image generation model and inject picture_v2 hint + prompt
        is_image = _is_image_model(model)
        system_hints = ["picture_v2"] if is_image else []
        if is_image and messages:
            last = messages[-1]
            if last.get("role") == "user":
                last["content"] = f"根据以下要求生成图片：{last['content']}"

        opts = ChatOptions(
            messages=messages,
            model=upstream_model,
            sse_timeout=self.sse_timeout,
            system_hints=system_hints,
        )

        # Set up tool stream parser if tools are provided
        tool_stream_enabled = bool(tools) and tool_choice != "none"
        tool_parser = ToolCallStreamParser(tools) if tool_stream_enabled else None
        tool_calls_seen = False
        tool_call_index = 0

        def _with_tool_index(tc: Dict[str, Any]) -> Dict[str, Any]:
            nonlocal tool_call_index
            if tc.get("index") is None:
                tc = dict(tc)
                tc["index"] = tool_call_index
                tool_call_index += 1
            return tc

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
            content_emitted = False

            for msg in client.chat_stream(opts):
                if msg.content:
                    if tool_parser:
                        # Once we've seen tool calls, discard remaining text
                        if tool_calls_seen:
                            continue
                        # Feed through tool parser
                        allow_calls = not content_emitted
                        for kind, payload in tool_parser.feed(msg.content, allow_calls=allow_calls):
                            if kind == "tool":
                                indexed_tc = _with_tool_index(payload)
                                tool_calls_seen = True
                                tc_chunk = {
                                    "id": chat_id,
                                    "object": "chat.completion.chunk",
                                    "created": created,
                                    "model": model,
                                    "choices": [{"index": 0, "delta": {"tool_calls": [indexed_tc]}, "finish_reason": None}],
                                }
                                yield f"data: {json.dumps(tc_chunk)}\n\n"
                            else:
                                # Text content before tool calls
                                if isinstance(payload, str) and payload.strip():
                                    content_emitted = True
                                content_len += len(payload)
                                content_chunk = {
                                    "id": chat_id,
                                    "object": "chat.completion.chunk",
                                    "created": created,
                                    "model": model,
                                    "choices": [{"index": 0, "delta": {"content": payload}, "finish_reason": None}],
                                }
                                yield f"data: {json.dumps(content_chunk)}\n\n"
                    else:
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

            # Flush remaining tool parser buffer
            if tool_parser and not tool_calls_seen:
                for kind, payload in tool_parser.flush():
                    if kind == "tool":
                        indexed_tc = _with_tool_index(payload)
                        tool_calls_seen = True
                        tc_chunk = {
                            "id": chat_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [{"index": 0, "delta": {"tool_calls": [indexed_tc]}, "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(tc_chunk)}\n\n"
                    elif isinstance(payload, str) and payload.strip():
                        content_len += len(payload)
                        content_chunk = {
                            "id": chat_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [{"index": 0, "delta": {"content": payload}, "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(content_chunk)}\n\n"

            # After stream ends, check for async image generation.
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

            finish_reason = "tool_calls" if tool_calls_seen else "stop"
            logger.info(f"Stream [{token.email}]: finished, content_len={content_len}, has_image={bool(image_url)}, tool_calls={tool_calls_seen}")
            finish_chunk = {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
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
