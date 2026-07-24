"""Server-owned background runs that survive client disconnect.

The problem this solves: an SSE endpoint that drives long work *inside* its
response generator (and persists after the progress loop) loses everything when
the browser navigates away — Starlette cancels the generator, so the work either
stops or finishes without ever being saved. Autoresearch, manuscript drafting,
and review generation all hit this.

The fix: a run is owned by a DETACHED worker task, not the request. The worker
runs to completion and PERSISTS on its own, whether or not anyone is listening.
The SSE endpoint merely tails the run's append-only event buffer via
``stream_run`` — cancelling that on disconnect stops only the listener, not the
run. Reconnecting replays the buffer from the start and then follows live, and
``status_payload`` lets a returning page discover an in-flight (or finished) run.

Usage (per endpoint):

    from .run_registry import Run, registry, stream_run, status_payload
    RUNS = registry("manuscript")            # namespace -> {slug: Run}

    # start (or attach to) a run
    existing = RUNS.get(slug)
    if existing and not existing.done:
        return StreamingResponse(stream_run(existing), media_type="text/event-stream")
    run = Run(); RUNS[slug] = run
    async def worker():
        try: ...; run.emit("step", "..."); ...; <persist>; run.push({"event":"complete",...})
        except Exception as e: run.emit("error", str(e)); run.finish("error"); return
        run.finish("complete", result_url=...)
    asyncio.create_task(worker())
    return StreamingResponse(stream_run(run), media_type="text/event-stream")

    # status endpoint
    return status_payload(RUNS.get(slug))
"""
import asyncio
import json

from fastapi.responses import StreamingResponse

# Headers that make Server-Sent Events actually stream live through nginx. Without
# X-Accel-Buffering, nginx buffers the response (proxy_buffering defaults on), so
# the browser sees no live "tail" — progress arrives only in flushed chunks or at
# the end. This header disables buffering for THIS response only, no nginx change.
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def sse_response(generator) -> StreamingResponse:
    """A text/event-stream response with the headers needed to stream live behind
    nginx. Use for every SSE endpoint instead of a bare StreamingResponse."""
    return StreamingResponse(generator, media_type="text/event-stream", headers=SSE_HEADERS)


class Run:
    """One background run: an append-only event buffer plus terminal status,
    shared between the detached worker (writer) and any SSE tail (readers)."""

    def __init__(self):
        self.events: list[dict] = []
        self.updated = asyncio.Event()
        self.done = False
        self.status = "running"          # running | complete | error
        self.result_url: str | None = None

    def push(self, ev: dict) -> None:
        """Append a full event dict (e.g. a 'complete' event carrying extra keys)."""
        self.events.append(ev)
        self.updated.set()

    def emit(self, event: str, detail) -> None:
        """Append a simple {event, detail} progress event."""
        self.push({"event": event, "detail": detail})

    def finish(self, status: str, result_url: str | None = None) -> None:
        self.status = status
        self.result_url = result_url
        self.done = True
        self.updated.set()


# namespace ("autoresearch" / "manuscript" / "reviews") -> {slug: Run}. Retained
# after completion so a returning client can see terminal status; overwritten when
# a new run starts for that slug.
_registries: dict[str, dict[str, Run]] = {}


def registry(namespace: str) -> dict[str, Run]:
    """The per-feature registry of runs, keyed by slug."""
    return _registries.setdefault(namespace, {})


async def stream_run(run: Run):
    """SSE generator tailing a run's buffer from the start (so a reconnect replays
    history) then following live until it finishes. Cancelling this on client
    disconnect does NOT stop the run — only this listener."""
    i = 0
    while True:
        run.updated.clear()
        while i < len(run.events):
            yield f"data: {json.dumps(run.events[i], default=str)}\n\n"
            i += 1
        if run.done:
            break
        try:
            await asyncio.wait_for(run.updated.wait(), timeout=15)
        except asyncio.TimeoutError:
            yield ": keepalive\n\n"   # heartbeat so proxies keep the stream open


def status_payload(run: Run | None) -> dict:
    """JSON body for a status endpoint: whether a run is live, or its terminal state."""
    if run and not run.done:
        return {"running": True, "n_events": len(run.events)}
    if run and run.done:
        return {"running": False, "status": run.status, "result_url": run.result_url}
    return {"running": False, "status": "idle"}
