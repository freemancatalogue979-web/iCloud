"""Mirror telemetry in server.py.

Run with: .venv/bin/python tests/test_telemetry.py   (or `npm test`)

A broken mirror and a working one look identical from the outside - the console
shows only HTTP access lines either way. These tests pin the diagnostics that
tell them apart, since that is the only signal available when something goes
wrong on a machine that is not this one.
"""

import asyncio
import contextlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("ok   " + name)
    else:
        failed += 1
        print("FAIL " + name + (("  " + detail) if detail else ""))


class StubPage:
    def __init__(self):
        self.calls = []

    async def evaluate(self, expr):
        self.calls.append(expr)


class BoomPage:
    async def evaluate(self, expr):
        raise RuntimeError("target closed")


def capture(*sizes):
    """A capture payload: the root frame plus one child per extra size."""
    frames = [{"windowId": "0", "html": "x" * sizes[0]}]
    frames += [
        {"windowId": f"0.{i}", "html": "x" * n}
        for i, n in enumerate(sizes[1:])
    ]
    return {"kind": "doc", "windowId": "0", "frames": frames}


async def main():
    # ---- the capture line is what reveals a missing frame ---------------
    line = server._describe([
        {"windowId": "0", "html": "x" * 400_000},
        {"windowId": "0.0", "html": "y" * 20_000},
    ])
    check("capture line lists every frame with its size",
          line == "2 frame(s) [0:390KB, 0.0:19KB]", line)
    check("an empty capture says so", server._describe([]) == "0 frames")
    check("a lone root frame is visible",
          server._describe([{"windowId": "0", "html": "z" * 10}]) == "1 frame(s) [0:0KB]")

    server.clients.clear()

    # ---- a capture is logged, with enough detail to spot a gap ----------
    server._stats.update(muts=0, ops=0, resyncs=0, last_beat=0.0)
    server.last_doc = None
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        await server.on_emit({}, capture(1024, 512))
    out = buf.getvalue()
    check("capture is logged with frame detail",
          "capture: 2 frame(s) [0:1KB, 0.0:0KB]" in out, out.strip())
    check("last_doc retained so late viewers get the page", server.last_doc is not None)

    # ---- mutations must not flood the console --------------------------
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for _ in range(250):
            await server.on_emit({}, {"kind": "mut", "windowId": "0",
                                      "ops": [{"op": "attr"}, {"op": "text"}]})
    out = buf.getvalue()
    check("250 mutations produce at most one log line", out.count("stream:") <= 1,
          repr(out[:200]))

    # ---- the throttled heartbeat reports the tally, then resets --------
    server._stats["last_beat"] -= 30
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        await server.on_emit({}, {"kind": "mut", "windowId": "0", "ops": [{"op": "add"}]})
    out = buf.getvalue()
    check("heartbeat reports updates and ops", "updates" in out and "ops" in out, out.strip())
    check("heartbeat reports the resync count", "resyncs" in out, out.strip())
    check("counters reset after a heartbeat", server._stats["muts"] == 0, str(server._stats))

    # ---- resync counting ------------------------------------------------
    saved = server.page
    try:
        server.page = None
        server._stats["resyncs"] = 0
        await server.request_resync()
        check("no browser session -> nothing counted", server._stats["resyncs"] == 0,
              str(server._stats))

        stub = StubPage()
        server.page = stub
        server._stats["resyncs"] = 0
        await server.request_resync()
        await server.request_resync()
        check("resyncs counted per request", server._stats["resyncs"] == 2, str(server._stats))
        check("resync reaches the page", len(stub.calls) == 2 and "__rbResync" in stub.calls[0],
              str(stub.calls))

        server.page = BoomPage()
        server._stats["resyncs"] = 0
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                await server.request_resync()
            check("a failing resync is swallowed, not raised", True)
        except Exception as exc:  # noqa: BLE001
            check("a failing resync is swallowed, not raised", False, repr(exc))
        check("failed resync still counted", server._stats["resyncs"] == 1, str(server._stats))
    finally:
        server.page = saved


asyncio.run(main())
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
