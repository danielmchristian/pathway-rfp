import asyncio

import pytest

from app.pipeline.events import BUFFER_PER_RESTAURANT, Event, EventBus, get_bus, stage


async def _next_n(gen, n: int, timeout: float = 1.0):
    out = []
    for _ in range(n):
        out.append(await asyncio.wait_for(gen.__anext__(), timeout=timeout))
    return out


@pytest.mark.asyncio
async def test_emit_after_subscribe_delivers_event() -> None:
    bus = EventBus()
    gen = bus.subscribe(1)
    # Subscribe registers on first iteration; force it.
    task = asyncio.create_task(gen.__anext__())
    await asyncio.sleep(0)  # let subscribe register the queue
    bus.emit(Event(restaurant_id=1, stage="menu_parse", status="start"))
    evt = await asyncio.wait_for(task, timeout=1.0)
    assert evt.name == "menu_parse:start"
    await gen.aclose()


@pytest.mark.asyncio
async def test_ring_buffer_replays_to_late_subscriber() -> None:
    bus = EventBus()
    for i in range(BUFFER_PER_RESTAURANT + 3):  # overflow the buffer
        bus.emit(Event(restaurant_id=42, stage="menu_parse", status="progress", payload={"i": i}))
    gen = bus.subscribe(42)
    replayed = await _next_n(gen, BUFFER_PER_RESTAURANT)
    # Should be the latest 10 events, in order.
    indices = [e.payload["i"] for e in replayed]
    assert indices == list(range(3, BUFFER_PER_RESTAURANT + 3))
    await gen.aclose()


@pytest.mark.asyncio
async def test_two_subscribers_both_receive_events() -> None:
    bus = EventBus()
    gen1 = bus.subscribe(7)
    gen2 = bus.subscribe(7)
    t1 = asyncio.create_task(gen1.__anext__())
    t2 = asyncio.create_task(gen2.__anext__())
    await asyncio.sleep(0)
    bus.emit(Event(restaurant_id=7, stage="menu_parse", status="complete"))
    e1, e2 = await asyncio.gather(t1, t2)
    assert e1.name == e2.name == "menu_parse:complete"
    await gen1.aclose()
    await gen2.aclose()


@pytest.mark.asyncio
async def test_stage_decorator_emits_start_and_complete() -> None:
    bus = get_bus()  # decorator uses the module singleton
    bus.reset()

    @stage("menu_parse")
    async def fake_stage(*, restaurant_id: int) -> dict:
        return {"dishes_inserted": 3}

    await fake_stage(restaurant_id=99)

    gen = bus.subscribe(99)
    events = await _next_n(gen, 2)
    await gen.aclose()
    assert [e.name for e in events] == ["menu_parse:start", "menu_parse:complete"]
    assert events[1].payload == {"dishes_inserted": 3}


@pytest.mark.asyncio
async def test_stage_decorator_emits_error_on_raise() -> None:
    bus = get_bus()
    bus.reset()

    @stage("menu_parse")
    async def boom(*, restaurant_id: int) -> None:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await boom(restaurant_id=11)

    gen = bus.subscribe(11)
    events = await _next_n(gen, 2)
    await gen.aclose()
    assert events[0].name == "menu_parse:start"
    assert events[1].name == "menu_parse:error"
    assert events[1].payload["error_type"] == "RuntimeError"
