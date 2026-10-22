"""Safe protocol failures and rate-limit metadata, without response bodies."""
from __future__ import annotations

from datetime import timezone
from email.utils import parsedate_to_datetime
import math
import time
from typing import Any


class ProtocolError(RuntimeError):
    """A safe error message, never containing credentials or response bodies."""


class RateLimitedError(ProtocolError):
    def __init__(self, retry_after: float = 30) -> None:
        try:
            delay = float(retry_after)
        except (TypeError, ValueError, OverflowError):
            delay = 30
        if not math.isfinite(delay):
            delay = 30
        self.retry_after = max(1, min(900, math.ceil(delay)))
        super().__init__(f"服务暂时限流（HTTP 429），约 {self.retry_after} 秒后可重试此账号")


def raise_for_rate_limit(response: Any) -> None:
    if response.status_code != 429:
        return
    value = getattr(response, "headers", {}).get("Retry-After", "")
    try:
        delay = float(value)
    except (TypeError, ValueError, OverflowError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = date.timestamp() - time.time()
        except (TypeError, ValueError, OverflowError, AttributeError):
            delay = 30
    raise RateLimitedError(delay)
