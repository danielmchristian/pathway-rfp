"""In-memory pub/sub for pipeline stage events.

Designed so multiple SSE subscribers per restaurant fan out cheaply, and new
subscribers see recent history via a small per-restaurant ring buffer.
Events are not persisted — DB rows are the source of truth for pipeline state.
"""

from __future__ import annotations

import asyncio
import functools
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

log = structlog.get_logger("events")

BUFFER_PER_RESTAURANT = 10
EventStatus = str  # "start" | "progress" | "complete" | "error"


@dataclass(frozen=True)
class Event:
    restaurant_id: int
    stage: str
    status: EventStatus
    payload: dict[str, Any] = field(default_factory=dict)
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def name(self) -> str:
        return f"{self.stage}:{self.status}"

    def to_json(self) -> dict[str, Any]:
        return {
            "restaurant_id": self.restaurant_id,
            "stage": self.stage,
            "status": self.status,
            "name": self.name,
            "payload": self.payload,
            "ts": self.ts.isoformat(),
        }


class EventBus:
    def __init__(self, buffer_size: int = BUFFER_PER_RESTAURANT) -> None:
        self._buffer_size = buffer_size
        self._subscribers: dict[int, set[asyncio.Queue[Event]]] = defaultdict(set)
        self._buffer: dict[int, deque[Event]] = defaultdict(lambda: deque(maxlen=self._buffer_size))

    def emit(self, event: Event) -> None:
        self._buffer[event.restaurant_id].append(event)
        for q in list(self._subscribers.get(event.restaurant_id, ())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:  # unbounded queues — defensive
                log.warning("event.dropped", name=event.name)

    async def subscribe(self, restaurant_id: int) -> AsyncIterator[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue()
        # Drain replay buffer first (last 10 events seen for this restaurant)
        for past in tuple(self._buffer.get(restaurant_id, ())):
            await q.put(past)
        self._subscribers[restaurant_id].add(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subscribers[restaurant_id].discard(q)
            if not self._subscribers[restaurant_id]:
                self._subscribers.pop(restaurant_id, None)

    def reset(self) -> None:
        """Test-only — clear subscribers and buffers."""
        self._subscribers.clear()
        self._buffer.clear()


_bus = EventBus()


def get_bus() -> EventBus:
    return _bus


def stage(stage_name: str) -> Callable[..., Any]:
    """Auto-emit start/complete/error events around an async service call.

    The wrapped function must accept `restaurant_id` as a kwarg or first positional.
    Return value is passed through unchanged.
    """

    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            rid = kwargs.get("restaurant_id")
            if rid is None and args:
                rid = args[0]
            if not isinstance(rid, int):
                raise TypeError(
                    f"@stage('{stage_name}') needs restaurant_id (int) as kwarg or first arg"
                )
            bus = get_bus()
            bus.emit(Event(restaurant_id=rid, stage=stage_name, status="start"))
            try:
                result = await func(*args, **kwargs)
            except Exception as exc:
                bus.emit(
                    Event(
                        restaurant_id=rid,
                        stage=stage_name,
                        status="error",
                        payload={"error_type": type(exc).__name__, "message": str(exc)},
                    )
                )
                raise
            bus.emit(
                Event(
                    restaurant_id=rid,
                    stage=stage_name,
                    status="complete",
                    payload=_safe_payload(result),
                )
            )
            return result

        return wrapper

    return decorator


def _safe_payload(value: Any) -> dict[str, Any]:
    """Best-effort: turn the service's return value into a JSON-serializable dict for the SSE payload."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return to_dict()
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return {
            k: (str(v) if not isinstance(v, int | float | str | bool | type(None)) else v)
            for k, v in vars(value).items()
        }
    return {"result": str(value)}
