"""LogBus: history, fan-out, and slow-consumer resilience."""

from __future__ import annotations

import asyncio

from app.events import LogBus


def test_emit_records_history_with_increasing_seq() -> None:
    bus = LogBus()
    first = bus.info("one")
    second = bus.info("two")
    assert (first.seq, second.seq) == (1, 2)
    assert [e.message for e in bus.history()] == ["one", "two"]


def test_history_since_seq_filters() -> None:
    bus = LogBus()
    bus.info("a")
    b = bus.info("b")
    bus.info("c")
    assert [e.message for e in bus.history(since_seq=b.seq)] == ["c"]


def test_history_is_bounded() -> None:
    bus = LogBus(history_limit=3)
    for i in range(10):
        bus.info(f"m{i}")
    assert [e.message for e in bus.history()] == ["m7", "m8", "m9"]


def test_sse_frame_is_well_formed() -> None:
    bus = LogBus()
    event = bus.info("hello", foo="bar")
    frame = event.sse()
    assert frame.startswith("event: log\ndata: ")
    assert frame.endswith("\n\n")
    assert '"hello"' in frame and '"bar"' in frame


async def test_subscriber_receives_new_events() -> None:
    bus = LogBus()
    bus.bind_loop(asyncio.get_running_loop())
    queue = bus.subscribe()

    bus.info("later")
    event = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert event.message == "later"
    assert bus.subscriber_count == 1

    bus.unsubscribe(queue)
    assert bus.subscriber_count == 0


async def test_slow_consumer_drops_oldest_instead_of_blocking() -> None:
    bus = LogBus(queue_limit=2)
    bus.bind_loop(asyncio.get_running_loop())
    queue = bus.subscribe()

    for i in range(5):
        bus.info(f"m{i}")
    # Let the marshalled put_nowait callbacks run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert queue.qsize() == 2
    drained = [queue.get_nowait().message for _ in range(2)]
    assert drained == ["m3", "m4"]


async def test_emit_from_thread_reaches_subscriber() -> None:
    """Selenium runs off-loop; events must still land."""
    bus = LogBus()
    bus.bind_loop(asyncio.get_running_loop())
    queue = bus.subscribe()

    def worker() -> None:
        bus.info("from thread")

    await asyncio.to_thread(worker)
    event = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert event.message == "from thread"


def test_levels_are_preserved() -> None:
    bus = LogBus()
    bus.warn("careful")
    bus.error("bad")
    bus.step("doing")
    assert [e.level for e in bus.history()] == ["warn", "error", "step"]
