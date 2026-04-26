"""
Tool call utilities for OpenAI-compatible function calling.

ChatGPT Web API does not expose native tool calling, so we translate tool
definitions into a constrained prompt contract and parse the model output
back into OpenAI-compatible tool_call payloads.

Adapted from grok2api.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple


CALL_START_TAG = "<call>"
CALL_END_TAG = "</call>"

_NEW_CALL_RE = re.compile(r"<call>\s*(.*?)\s*</call>", re.DOTALL)


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _tool_specs(tools: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    specs: List[Dict[str, Any]] = []
    if not tools:
        return specs

    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") != "function":
            continue

        if isinstance(tool.get("function"), dict):
            func = tool["function"]
            name = func.get("name")
            description = func.get("description")
            parameters = func.get("parameters") or {}
            strict = func.get("strict")
        else:
            name = tool.get("name")
            description = tool.get("description")
            parameters = tool.get("parameters") or {}
            strict = tool.get("strict")

        if not isinstance(name, str) or not name.strip():
            continue

        specs.append({
            "name": name.strip(),
            "description": (description or "").strip(),
            "parameters": parameters if isinstance(parameters, dict) else {},
            "strict": bool(strict),
        })

    return specs


def _valid_tool_names(tools: Optional[List[Dict[str, Any]]]) -> set[str]:
    return {spec["name"] for spec in _tool_specs(tools)}


def _forced_tool_name(tool_choice: Optional[Any]) -> Optional[str]:
    if not isinstance(tool_choice, dict):
        return None
    if isinstance(tool_choice.get("function"), dict):
        name = tool_choice["function"].get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    name = tool_choice.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def build_tool_prompt(
    tools: List[Dict[str, Any]],
    tool_choice: Optional[Any] = None,
    parallel_tool_calls: bool = True,
) -> str:
    """Generate a compact tool-calling contract for the model."""
    specs = _tool_specs(tools)
    if not specs or tool_choice == "none":
        return ""

    lines = [
        "# Tool Calling Contract",
        "",
        "You may use the tools below when they are necessary to complete the user's request.",
        "When you call a tool, your entire reply must contain only one or more tool blocks.",
        "Do not add prose, markdown, code fences, explanations, or text before, between, or after tool blocks.",
        "",
        "Exact tool block format:",
        "<call>",
        "tool_name",
        '{"arg":"value"}',
        "</call>",
        "",
        "Rules:",
        "1. The first line must be exactly <call>.",
        "2. The second line must be exactly the tool name.",
        "3. The third line must be a single valid JSON object containing only tool arguments.",
        "4. Do not include trailing commas, comments, or markdown fences.",
        "5. If you do not need a tool, reply with normal text and do not emit any tool block.",
        "6. If required fields are missing and a valid call cannot be produced, ask a concise clarification question instead of inventing values.",
    ]

    if parallel_tool_calls:
        lines.append("7. You may emit multiple <call> blocks when multiple tool calls are required.")
    else:
        lines.append("7. Emit at most one <call> block.")

    if tool_choice == "required":
        lines.append("8. You must emit at least one valid <call> block and must not answer with prose only.")
    elif forced_name := _forced_tool_name(tool_choice):
        lines.append(f'8. If you emit a tool block, it must use only the tool name "{forced_name}".')
    else:
        lines.append("8. Only call a tool when it materially helps answer the user.")

    lines.extend(["", "Available tools:"])

    for spec in specs:
        lines.append(f'- {spec["name"]}: {spec["description"] or "No description provided."}')
        lines.append(f'  parameters: {_compact_json(spec["parameters"])}')
        if spec["strict"]:
            lines.append("  strict: true")

    lines.extend([
        "",
        "Examples:",
        "Tool call:",
        "<call>",
        specs[0]["name"],
        '{"example":"value"}',
        "</call>",
        "",
        "No tool needed:",
        "Reply with normal user-facing text and do not emit any <call> block.",
    ])

    return "\n".join(lines)


def format_tool_history(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert tool-related message history into text form ChatGPT can consume."""
    result = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content")
        tool_calls = msg.get("tool_calls")
        tool_call_id = msg.get("tool_call_id")
        name = msg.get("name")

        if role == "assistant" and tool_calls:
            parts: List[str] = []
            if content:
                parts.append(content if isinstance(content, str) else str(content))
            for tool_call in tool_calls:
                function = tool_call.get("function", {}) or {}
                tc_name = function.get("name", "")
                tc_args = function.get("arguments", "{}")
                if not isinstance(tc_args, str):
                    tc_args = json.dumps(tc_args, ensure_ascii=False)
                parts.append("\n".join([CALL_START_TAG, tc_name, tc_args.strip(), CALL_END_TAG]))
            result.append({"role": "assistant", "content": "\n".join(parts)})
            continue

        if role == "tool":
            tool_name = name or "unknown"
            call_id = tool_call_id or ""
            if isinstance(content, str):
                content_str = content
            elif content:
                content_str = json.dumps(content, ensure_ascii=False)
            else:
                content_str = ""
            result.append({
                "role": "user",
                "content": f"tool ({tool_name}, {call_id}): {content_str}",
            })
            continue

        result.append(msg)

    return result


# ---------- JSON repair helpers ----------

def _strip_code_fences(text: str) -> str:
    if not text:
        return text
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _extract_json_object(text: str) -> str:
    if not text:
        return text
    start = text.find("{")
    if start == -1:
        return text
    end = text.rfind("}")
    if end == -1:
        return text[start:]
    if end < start:
        return text
    return text[start:end + 1]


def _remove_trailing_commas(text: str) -> str:
    if not text:
        return text
    return re.sub(r",\s*([}\]])", r"\1", text)


def _balance_braces(text: str) -> str:
    if not text:
        return text
    open_count = 0
    close_count = 0
    in_string = False
    escape = False
    for ch in text:
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            open_count += 1
        elif ch == "}":
            close_count += 1
    if open_count > close_count:
        text = text + ("}" * (open_count - close_count))
    return text


def _repair_json(text: str) -> Optional[Any]:
    if not text:
        return None
    cleaned = _strip_code_fences(text)
    cleaned = _extract_json_object(cleaned)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _remove_trailing_commas(cleaned)
    cleaned = _balance_braces(cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return None


def _make_tool_call(name: str, arguments: Any) -> Dict[str, Any]:
    if isinstance(arguments, str):
        arguments_text = arguments
    else:
        arguments_text = json.dumps(arguments, ensure_ascii=False)
    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {"name": name, "arguments": arguments_text},
    }


def parse_tool_call_block(
    raw_block: str,
    tools: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Parse a single <call> block content into a tool_call dict."""
    if not raw_block:
        return None
    lines = raw_block.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) < 2:
        return None

    name = lines[0].strip()
    if not name:
        return None

    valid_names = _valid_tool_names(tools)
    if valid_names and name not in valid_names:
        return None

    arguments_text = "\n".join(lines[1:]).strip()
    if not arguments_text:
        return None

    try:
        parsed_arguments = json.loads(arguments_text)
    except json.JSONDecodeError:
        parsed_arguments = _repair_json(arguments_text)

    if not isinstance(parsed_arguments, dict):
        return None

    return _make_tool_call(name, parsed_arguments)


def _text_outside_blocks(
    content: str,
    matches: List[re.Match[str]],
) -> str:
    parts: List[str] = []
    last_end = 0
    for match in matches:
        before = content[last_end:match.start()]
        if before:
            parts.append(before)
        last_end = match.end()
    trailing = content[last_end:]
    if trailing:
        parts.append(trailing)
    return "".join(parts)


def parse_tool_calls(
    content: str,
    tools: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Optional[str], Optional[List[Dict[str, Any]]]]:
    """Parse tool call blocks from model output.

    Returns (text_content, tool_calls) where text_content is the remaining
    text after extracting tool calls (may be None), and tool_calls is None
    if no valid tool calls were found.

    Lenient: extracts valid <call> blocks even when surrounded by prose.
    """
    if not content:
        return content, None

    matches = list(_NEW_CALL_RE.finditer(content))
    if not matches:
        return content, None

    # Extract all valid tool calls from <call> blocks
    tool_calls: List[Dict[str, Any]] = []
    for match in matches:
        tool_call = parse_tool_call_block(match.group(1), tools)
        if tool_call:
            tool_calls.append(tool_call)

    if not tool_calls:
        return content, None

    # Collect text outside the valid call blocks
    text_parts = []
    last_end = 0
    for match in matches:
        before = content[last_end:match.start()]
        if before.strip():
            text_parts.append(before.strip())
        last_end = match.end()
    trailing = content[last_end:]
    if trailing.strip():
        text_parts.append(trailing.strip())

    text_content = "\n".join(text_parts) if text_parts else None
    return text_content, tool_calls


# ---------- Streaming parser ----------

class ToolCallStreamParser:
    """Incrementally parse <call> blocks from streamed text."""

    def __init__(self, tools: Optional[List[Dict[str, Any]]] = None):
        self.tools = tools
        self._mode = "text"  # "text" or "tool"
        self._buffer = ""
        self._partial = ""

    @staticmethod
    def _suffix_prefix(text: str, tag: str) -> int:
        """How many chars at end of text could be the start of tag."""
        if not text or not tag:
            return 0
        max_keep = min(len(text), len(tag) - 1)
        for keep in range(max_keep, 0, -1):
            if text.endswith(tag[:keep]):
                return keep
        return 0

    def feed(self, chunk: str, *, allow_calls: bool = True) -> List[Tuple[str, Any]]:
        """Feed a text chunk. Returns list of ('text', str) or ('tool', dict) events."""
        events: List[Tuple[str, Any]] = []
        if not chunk:
            return events

        data = f"{self._partial}{chunk}"
        self._partial = ""

        while data:
            if self._mode == "text":
                if not allow_calls:
                    events.append(("text", data))
                    break

                idx = data.find(CALL_START_TAG)
                if idx == -1:
                    keep = self._suffix_prefix(data, CALL_START_TAG)
                    emit = data[:-keep] if keep else data
                    if emit:
                        events.append(("text", emit))
                    self._partial = data[-keep:] if keep else ""
                    break

                before = data[:idx]
                if before:
                    events.append(("text", before))

                data = data[idx + len(CALL_START_TAG):]
                self._mode = "tool"
                continue

            # mode == "tool": looking for </call>
            end_idx = data.find(CALL_END_TAG)
            if end_idx == -1:
                keep = self._suffix_prefix(data, CALL_END_TAG)
                append = data[:-keep] if keep else data
                if append:
                    self._buffer += append
                self._partial = data[-keep:] if keep else ""
                break

            self._buffer += data[:end_idx]
            data = data[end_idx + len(CALL_END_TAG):]
            tool_call = parse_tool_call_block(self._buffer, self.tools)
            if tool_call:
                events.append(("tool", tool_call))
            else:
                events.append(("text", f"{CALL_START_TAG}{self._buffer}{CALL_END_TAG}"))
            self._buffer = ""
            self._mode = "text"

        return events

    def flush(self) -> List[Tuple[str, Any]]:
        """Flush any remaining buffered content."""
        events: List[Tuple[str, Any]] = []
        if self._mode == "text":
            if self._partial:
                events.append(("text", self._partial))
                self._partial = ""
            return events

        raw = f"{self._buffer}{self._partial}"
        tool_call = parse_tool_call_block(self._buffer, self.tools)
        if tool_call:
            events.append(("tool", tool_call))
        elif raw:
            events.append(("text", f"{CALL_START_TAG}{raw}{CALL_END_TAG}"))
        self._buffer = ""
        self._partial = ""
        self._mode = "text"
        return events
