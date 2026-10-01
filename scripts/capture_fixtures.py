"""Capture controlled Gufo fixtures; sends 15 inference requests and overwrites files."""

from __future__ import annotations

import argparse
import base64
import json
import os
import struct
import urllib.error
import urllib.request
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "gufo"


def png(width: int = 16, height: int = 16) -> bytes:
    """Make a small red RGB PNG without an image dependency."""
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def capture(base: str, image: str, name: str, method: str, path: str, body: Any = None) -> None:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            status, headers, payload = response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        with exc:
            status, headers, payload = exc.code, exc.headers, exc.read()
    content_type = headers.get("Content-Type", "")
    extension = (
        "sse" if "event-stream" in content_type else ("json" if "json" in content_type else "txt")
    )
    (OUT / f"{name}.{extension}").write_bytes(payload)
    metadata = {
        "method": method,
        "path": path,
        "request": body,
        "status": status,
        "headers": dict(headers),
    }
    (OUT / f"{name}.meta.json").write_text(
        json.dumps(metadata, indent=2).replace(image, "<16x16 red PNG data URI>") + "\n"
    )
    print(f"{name:32} {status} {content_type} {len(payload)}B")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gufo-version", required=True, help="Upstream version or build label")
    parser.add_argument("--gufo-commit", help="Upstream source commit, if known")
    args = parser.parse_args()
    base = os.environ.get("GUFO_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
    with urllib.request.urlopen(base + "/ready", timeout=180) as response:
        model = json.load(response)["model"]
    image = "data:image/png;base64," + base64.b64encode(png()).decode()
    messages = [{"role": "user", "content": "Say hi"}]
    chat = {"model": model, "messages": messages, "max_tokens": 16}
    completion = {"model": model, "prompt": "Hello", "max_tokens": 16}
    responses = {"model": model, "input": "Say hi", "max_output_tokens": 16}
    native = {"prompt": "Hello", "n_predict": 16}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "gufo_build.json").write_text(
        json.dumps(
            {
                "gufo_version": args.gufo_version,
                "gufo_commit": args.gufo_commit,
                "captured_at": datetime.now(UTC).isoformat(),
                "source": "operator-supplied",
            },
            indent=2,
        )
        + "\n"
    )

    def req(name: str, method: str, path: str, body: Any = None) -> None:
        capture(base, image, name, method, path, body)

    for name, path in (
        ("health", "/health"),
        ("ready", "/ready"),
        ("models", "/v1/models"),
        ("metrics_before", "/metrics"),
    ):
        req(name, "GET", path)
    req("chat_nonstream", "POST", "/v1/chat/completions", chat)
    req("chat_nonstream_cachehit", "POST", "/v1/chat/completions", chat)
    req("chat_stream_no_usage", "POST", "/v1/chat/completions", {**chat, "stream": True})
    req(
        "chat_stream_include_usage",
        "POST",
        "/v1/chat/completions",
        {**chat, "stream": True, "stream_options": {"include_usage": True}},
    )
    req(
        "chat_vision_nonstream",
        "POST",
        "/v1/chat/completions",
        {
            "model": model,
            "max_tokens": 16,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What colour is this?"},
                        {"type": "image_url", "image_url": {"url": image}},
                    ],
                }
            ],
        },
    )
    req(
        "completions_stream_include_usage",
        "POST",
        "/v1/completions",
        {**completion, "stream": True, "stream_options": {"include_usage": True}},
    )
    req("responses_nonstream", "POST", "/v1/responses", responses)
    req("responses_stream", "POST", "/v1/responses", {**responses, "stream": True})
    req("messages_nonstream", "POST", "/v1/messages", chat)
    req("messages_stream", "POST", "/v1/messages", {**chat, "stream": True})
    req("native_completion_stream", "POST", "/completion", {**native, "stream": True})
    req("error_bad_json", "POST", "/v1/chat/completions")
    req(
        "error_unknown_model",
        "POST",
        "/v1/chat/completions",
        {**chat, "model": "nope", "max_tokens": 4},
    )
    req("metrics_after", "GET", "/metrics")
    # Keep these outside the before/after metrics window used by the fixture tests.
    req("completions_nonstream", "POST", "/v1/completions", completion)
    req("native_completion_nonstream", "POST", "/completion", native)


if __name__ == "__main__":
    main()
