"""Opt-in, bounded text capture. Never retain raw payloads or non-text blocks."""

from __future__ import annotations

import json
from typing import Any


class TextBuffer:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.parts: list[str] = []
        self.size = 0
        self.truncated = False

    def append(self, value: Any) -> None:
        if not isinstance(value, str) or not value:
            return
        # Slice characters before encoding to avoid copying a huge string.
        room = self.limit - self.size
        raw = value[:room].encode("utf-8")
        kept = raw[:room].decode("utf-8", "ignore")
        self.truncated |= len(kept) < len(value)
        if kept:
            self.parts.append(kept)
            self.size += len(kept.encode("utf-8"))

    def text(self) -> str | None:
        return "".join(self.parts) or None


def text_parts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [
            p["text"]
            for p in value
            if isinstance(p, dict)
            and p.get("type") in ("text", "input_text", "output_text")
            and isinstance(p.get("text"), str)
        ]
    return []


class ContentCapture:
    def __init__(self, kind: str, body: bytes, enabled: bool, limit: int) -> None:
        self.kind = kind
        self.enabled = enabled
        self.question = TextBuffer(limit)
        self.answer = TextBuffer(limit)
        self.supported = False
        self.failed = False
        self.terminal = False
        self.incomplete = False
        if not enabled:
            return
        try:
            obj = json.loads(body)
            if not isinstance(obj, dict):
                return
            value: Any = None
            if kind in ("chat", "messages"):
                messages = obj.get("messages")
                if isinstance(messages, list):
                    for message in reversed(messages):
                        if isinstance(message, dict) and message.get("role") == "user":
                            value = message.get("content")
                            break
            elif kind == "responses":
                value = obj.get("input")
                if isinstance(value, list) and not text_parts(value):
                    value = next(
                        (
                            m.get("content")
                            for m in reversed(value)
                            if isinstance(m, dict) and m.get("role") == "user"
                        ),
                        None,
                    )
            elif kind in ("completions", "native"):
                value = obj.get("prompt")
            for part in text_parts(value):
                self.question.append(part)
            self.supported = self.question.size > 0
        except (ValueError, RecursionError):
            self.failed = True

    def body(self, obj: Any) -> None:
        if not self.enabled or not isinstance(obj, dict):
            return
        self.terminal = True
        if self.kind in ("chat", "completions"):
            choices = obj.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                choice = choices[0]
                message = choice.get("message", {})
                value = message.get("content") if isinstance(message, dict) else None
                if self.kind == "completions":
                    value = choice.get("text")
                self._answer(value)
        elif self.kind == "responses":
            self.incomplete = obj.get("status") in ("incomplete", "failed")
            output = obj.get("output")
            if isinstance(output, list):
                for item in output:
                    if (
                        isinstance(item, dict)
                        and item.get("type") == "message"
                        and item.get("role") == "assistant"
                    ):
                        self._answer(item.get("content"))
        elif self.kind == "messages" or self.kind == "native":
            self._answer(obj.get("content"))

    def _answer(self, value: Any) -> None:
        for part in text_parts(value):
            self.answer.append(part)
            self.supported = True

    def event(self, data: str) -> None:
        if not self.enabled:
            return
        if data == "[DONE]":
            self.terminal = True
            return
        try:
            obj = json.loads(data)
        except (ValueError, RecursionError):
            self.failed = True
            return
        if not isinstance(obj, dict):
            return
        if self.kind in ("chat", "completions"):
            choices = obj.get("choices")
            if isinstance(choices, list):
                for choice in choices:
                    if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                        continue
                    delta = choice.get("delta", {})
                    self._answer(
                        choice.get("text")
                        if self.kind == "completions"
                        else delta.get("content")
                        if isinstance(delta, dict)
                        else None
                    )
                    if choice.get("finish_reason"):
                        self.terminal = True
        elif self.kind == "responses":
            typ = obj.get("type")
            if typ == "response.output_text.delta":
                self._answer(obj.get("delta"))
            if typ in ("response.completed", "response.incomplete", "response.failed"):
                self.terminal = True
                self.incomplete = typ != "response.completed"
                # Terminal output repeats streamed text: do not append it again.
                if not self.answer.size:
                    self.body(obj.get("response"))
        elif self.kind == "messages":
            if obj.get("type") == "content_block_start":
                block = obj.get("content_block", {})
                if isinstance(block, dict) and block.get("type") == "text":
                    self._answer(block.get("text"))
            elif obj.get("type") == "content_block_delta":
                delta = obj.get("delta", {})
                if isinstance(delta, dict) and delta.get("type") == "text_delta":
                    self._answer(delta.get("text"))
            elif obj.get("type") == "message_stop":
                self.terminal = True
        elif self.kind == "native":
            self._answer(obj.get("content"))
            self.terminal |= obj.get("stop") is True

    def record(self, *, complete: bool, failed: bool = False) -> dict[str, Any]:
        if not self.enabled:
            status = "disabled"
        elif failed or self.failed or self.incomplete or not complete or not self.terminal:
            status = "partial"
        elif not self.supported:
            status = "unsupported"
        else:
            status = "complete"
        return {
            "status": status,
            "question": self.question.text(),
            "answer": self.answer.text(),
            "question_truncated": self.question.truncated,
            "answer_truncated": self.answer.truncated,
        }
