"""Incremental Server-Sent Events parser working on raw bytes.

The parser never alters bytes: every event it returns carries `raw`, the exact
bytes of that event including its blank-line terminator, so a caller can
forward the stream event-by-event (or drop one event) and the concatenation of
the forwarded `raw` values equals the upstream body minus the dropped events.

Line terminators may be `\\n`, `\\r\\n` or `\\r` (per the SSE spec) and events
may be split across network chunks at any byte.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_EOL = re.compile(rb"[\r\n]")

# An SSE event larger than this (without a blank line) stops inspection.
DEFAULT_MAX_EVENT_BYTES = 16 * 1024 * 1024


@dataclass(slots=True)
class SSEEvent:
    raw: bytes
    event: str | None
    data: str | None  # joined `data:` lines; None when the event had no data field


class SSEParser:
    def __init__(self, max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES) -> None:
        self._buf = bytearray()
        self._scan = 0  # where to look for the next line terminator
        self._line_start = 0
        self._lines: list[bytes] = []
        self._max = max_event_bytes
        self.overflowed = False

    def feed(self, chunk: bytes) -> list[SSEEvent]:
        """Add bytes and return every event completed by them."""
        if self.overflowed:
            return []
        buf = self._buf
        buf += chunk
        events: list[SSEEvent] = []
        pos = self._scan
        while True:
            m = _EOL.search(buf, pos)
            if m is None:
                break
            n = m.start()
            if buf[n] == 0x0D:  # \r: may be the first half of \r\n
                if n + 1 >= len(buf):
                    break
                end = n + 2 if buf[n + 1] == 0x0A else n + 1
            else:
                end = n + 1
            line = bytes(buf[self._line_start : n])
            if line:
                self._lines.append(line)
                self._line_start = end
                pos = end
                continue
            # Blank line: dispatch the event, including this terminator.
            events.append(_build(bytes(buf[:end]), self._lines))
            del buf[:end]
            self._lines = []
            self._line_start = 0
            pos = 0
        self._scan = pos
        if len(buf) > self._max:
            self.overflowed = True
        return events

    def take_remainder(self) -> bytes:
        """Return (and forget) bytes of an incomplete trailing event."""
        rest = bytes(self._buf)
        self._buf = bytearray()
        self._scan = self._line_start = 0
        self._lines = []
        return rest


def parse_remainder(raw: bytes) -> SSEEvent | None:
    """Best-effort parse of an unterminated final event (for stats only)."""
    if not raw.strip():
        return None
    return _build(raw, [ln for ln in re.split(rb"\r\n|\r|\n", raw) if ln])


def _build(raw: bytes, lines: list[bytes]) -> SSEEvent:
    event: str | None = None
    data: list[str] = []
    for line in lines:
        if line.startswith(b":"):
            continue
        name, sep, value = line.partition(b":")
        if sep and value.startswith(b" "):
            value = value[1:]
        if name == b"data":
            data.append(value.decode("utf-8", "replace"))
        elif name == b"event":
            event = value.decode("utf-8", "replace")
    return SSEEvent(raw=raw, event=event, data="\n".join(data) if data else None)
