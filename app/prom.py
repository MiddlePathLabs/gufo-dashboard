"""Minimal Prometheus text-format parser (no dependency).

Only what the dashboard needs: `name{labels} value [timestamp]` samples.
Comments, blank lines and malformed lines are skipped; unknown metrics are kept.
"""

from __future__ import annotations

import math
import re

_SAMPLE = re.compile(
    r"^(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)"
    r"(?P<labels>\{[^}]*\})?"
    r"\s+(?P<value>\S+)"
    r"(?:\s+(?P<ts>-?\d+))?\s*$"
)


def parse_prometheus(text: str) -> dict[str, float]:
    """Return `{name or name{labels}: value}`; later duplicates win."""
    out: dict[str, float] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE.match(line)
        if not m:
            continue
        try:
            value = float(m.group("value"))
        except ValueError:
            continue
        if not math.isfinite(value):
            continue
        key = m.group("name") + (m.group("labels") or "")
        out[key] = value
    return out


PROMPT_TOTAL = "llamacpp:prompt_tokens_total"
PREDICTED_TOTAL = "llamacpp:tokens_predicted_total"
PROMPT_RATE = "llamacpp:prompt_tokens_seconds"
PREDICTED_RATE = "llamacpp:predicted_tokens_seconds"

REQUESTS_PROCESSING = "llamacpp:requests_processing"
REQUESTS_DEFERRED = "llamacpp:requests_deferred"


def prompt_excludes_cached(text: str) -> bool:
    """Gufo 0.4 announces its new counter units in the HELP line."""
    return any(
        line.startswith(f"# HELP {PROMPT_TOTAL} ") and "excluding cache hits" in line
        for line in text.splitlines()
    )
