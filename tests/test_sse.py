"""Unit tests for SSE citation stripping and finish handling."""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.chatgpt.client import ChatGPTClient, ChatOptions
from app.chatgpt.sse import (
    CitationStripper,
    ChatMessage,
    SSEEvent,
    extract_chat_messages,
    strip_citation_markers,
)


def _make_sse_event(data: dict, event: str = "delta") -> SSEEvent:
    return SSEEvent(event=event, data=json.dumps(data))


class CitationStripperTests(unittest.TestCase):
    def test_bare_text(self) -> None:
        self.assertEqual(
            strip_citation_markers("Hello citeturn0finance0 world"),
            "Hello  world",
        )

    def test_across_chunks_bare_text(self) -> None:
        """Bare-text citation tokens split across SSE chunks are still removed."""
        stripper = CitationStripper()
        out1 = stripper.feed("Hello citeturn0fi")
        out2 = stripper.feed("nance0 world")
        out3 = stripper.flush()
        combined = (out1 + out2 + out3).strip()
        self.assertNotIn("citeturn0finance0", combined)
        self.assertIn("Hello", combined)
        self.assertIn("world", combined)

    def test_across_chunks_pua(self) -> None:
        """PUA-delimited markers split across SSE chunks are still removed."""
        stripper = CitationStripper()
        out1 = stripper.feed("Hello \ue200cite\ue202turn0")
        out2 = stripper.feed("finance0\ue201 world")
        out3 = stripper.flush()
        combined = (out1 + out2 + out3).strip()
        self.assertNotIn("\ue200", combined)
        self.assertNotIn("\ue201", combined)
        self.assertNotIn("citeturn0finance0", combined)
        self.assertIn("Hello", combined)
        self.assertIn("world", combined)


class FinishContextTests(unittest.TestCase):
    def test_term_finish_uses_message_context(self) -> None:
        """Batch JSON Patch finish signals use per-message context."""
        events = [
            # delta_encoding add branch: tool message stores its context first.
            _make_sse_event({
                "conversation_id": "c1",
                "message_id": "tool1",
                "type": "delta_encoding",
                "p": "",
                "o": "add",
                "v": {
                    "conversation_id": "c1",
                    "message": {
                        "id": "tool1",
                        "author": {"role": "tool"},
                        "recipient": "all",
                        "content": {"content_type": "text", "parts": ["searching..."]},
                        "status": "finished_successfully",
                    }
                }
            }),
            # Assistant answer via batch JSON Patch.
            _make_sse_event({
                "conversation_id": "c1",
                "message_id": "ans1",
                "v": [
                    {"p": "/message/content/parts/0", "o": "append", "v": "42"},
                    {"p": "/message/status", "o": "append", "v": "finished_successfully"},
                ]
            }),
        ]
        messages = list(extract_chat_messages(iter(events)))
        self.assertFalse(any(m.message_id == "tool1" for m in messages))
        self.assertTrue(any(m.message_id == "ans1" and m.finish_reason == "stop" for m in messages))


class ChatStreamFlushTests(unittest.TestCase):
    def test_flushes_on_terminal_empty_content(self) -> None:
        """A terminal message with empty content still emits buffered citation tail."""
        async def _run() -> None:
            client = ChatGPTClient(access_token="dummy")
            client.sentinel.bootstrap = AsyncMock()
            req = MagicMock()
            req.token = "token"
            req.proofofwork_required = False
            client.sentinel.get_chat_requirements = AsyncMock(return_value=req)
            client.prepare_fchat = AsyncMock(return_value="conduit")

            async def fake_stream(*args, **kwargs):
                # First chunk leaves a partial citation token in the stripper tail.
                yield ChatMessage(content="Hello citeturn0fi", finish_reason="")
                # Terminal chunk has no content; the previous tail should still be flushed.
                yield ChatMessage(content="", finish_reason="stop")

            client.stream_fchat = fake_stream
            opts = ChatOptions(messages=[{"role": "user", "content": "hi"}])
            messages = [m async for m in client.chat_stream(opts)]

            self.assertEqual(len(messages), 1)
            terminal = messages[0]
            self.assertEqual(terminal.finish_reason, "stop")
            # The stripper buffered "Hello citeturn0fi" and flushed it on termination.
            self.assertEqual(terminal.content, "Hello citeturn0fi")

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
