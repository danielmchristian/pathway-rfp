"""Hand-rolled httpx retry helper. No tenacity — too small a surface area to pull in a dep for.

Retries 5xx, 429, timeouts, and connect errors with exponential backoff + jitter.
On final failure: returns None and logs the failure. Caller decides fallback semantics.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx
import structlog

log = structlog.get_logger("http_retry")

T = TypeVar("T")

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 0.5  # seconds
DEFAULT_MAX_DELAY = 8.0
DEFAULT_TIMEOUT = 15.0  # seconds per request

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_RETRYABLE_EXC = (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError)


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    label: str | None = None,
    **kwargs,
) -> httpx.Response | None:
    """Issue an HTTP request with exponential-backoff retries.

    Returns the final Response on success (any 2xx/3xx/4xx-other-than-429),
    or None if every attempt failed.
    """
    ctx = label or f"{method} {url}"
    last_exc: Exception | None = None
    last_status: int | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            response = await client.request(method, url, **kwargs)
        except _RETRYABLE_EXC as exc:
            last_exc = exc
            last_status = None
            log.warning(
                "http.retry.exception",
                label=ctx,
                attempt=attempt,
                exc_type=type(exc).__name__,
                msg=str(exc),
            )
        else:
            if response.status_code in _RETRYABLE_STATUS:
                last_exc = None
                last_status = response.status_code
                log.warning(
                    "http.retry.status",
                    label=ctx,
                    attempt=attempt,
                    status=response.status_code,
                )
            else:
                return response

        if attempt == max_attempts:
            break
        delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
        delay += random.uniform(0, delay * 0.25)  # 25% jitter
        await asyncio.sleep(delay)

    log.error(
        "http.retry.exhausted",
        label=ctx,
        attempts=max_attempts,
        last_status=last_status,
        last_exc=type(last_exc).__name__ if last_exc else None,
    )
    return None


async def with_attempts(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    label: str = "task",
) -> T | None:
    """Retry an arbitrary async callable. Returns None if every attempt raises."""
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn()
        except Exception as exc:
            last_exc = exc
            log.warning(
                "task.retry.exception",
                label=label,
                attempt=attempt,
                exc_type=type(exc).__name__,
                msg=str(exc),
            )
            if attempt == max_attempts:
                break
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay += random.uniform(0, delay * 0.25)
            await asyncio.sleep(delay)
    log.error("task.retry.exhausted", label=label, attempts=max_attempts, last_exc=str(last_exc))
    return None
