"""
The web Body's HTTP surface: four endpoints and a directory of static
files, served by FastAPI/uvicorn on 127.0.0.1.

Why a framework rather than http.server: SSE, multipart MJPEG and
threadpool offloading of blocking handlers are all standard things a real
framework already does correctly, and hand-rolling them is how you end up
with a server that stalls the whole page behind one slow Gemini call.

Transport choices, all deliberate:

  GET  /                the page itself (static files)
  GET  /events          SSE — state, narration, stats, errors. One-way,
                        auto-reconnecting, and no WebSocket dependency.
  GET  /stream.mjpg     multipart/x-mixed-replace — the classic MJPEG
                        pattern, which an <img> renders with no JS at all.
  POST /ask             the recorded WAV; returns {transcript, text}
  POST /exchange/begin  open the narration-suppression window
  POST /exchange/end    close it

The exchange endpoints exist because this Body records in the browser and
speaks with the browser's speechSynthesis, so one exchange spans several
requests and there is no single block on the server to wrap. See
bodies/gate.py.

Everything is bound to 127.0.0.1 and there is no authentication. That is
a considered choice, not an oversight: this is a local personal system,
and exposing it on the LAN would need both auth and TLS before it was
safe. It is also why a LAN address will not work — getUserMedia refuses a
non-secure origin, so the microphone is unavailable from another device
anyway.

Run with:  uv run python run_aura.py --web
"""

from __future__ import annotations

import asyncio
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from fastapi import Body, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).resolve().parent / "static"

HOST = "127.0.0.1"
PORT = 8000

MJPEG_BOUNDARY = "auraframe"
MJPEG_CONTENT_TYPE = f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}"

# The MJPEG generator polls rather than being pushed to. At 30 ms it is
# ~33 fps, above the capture rate, so the browser always gets the newest
# frame with at most one poll of latency — and one small sleep per client
# is cheaper than a per-client push queue feeding an <img> that cannot
# consume faster than it renders anyway.
FRAME_POLL_SECONDS = 0.03

# SSE comment lines keep proxies and browsers from timing out a quiet
# stream. Narration can be silent for minutes when nothing is happening.
SSE_KEEPALIVE_SECONDS = 15.0


def create_app(body) -> FastAPI:
    """
    Builds the app around an already-constructed Body.

    Takes the Body as an argument rather than importing a concrete one, so
    a test can hand in a fake and exercise every route without a camera, a
    model, Redis or an API key.
    """
    app = FastAPI(
        title="AURA",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/events")
    async def events(request: Request) -> StreamingResponse:
        sub = body.state.subscribe()

        async def stream():
            try:
                # An immediate line so the browser's EventSource fires
                # onopen straight away instead of after the first event —
                # otherwise "connected" and "nothing is happening yet"
                # look the same in the UI.
                yield ": aura web body connected\n\n"

                while True:
                    try:
                        event, data = await asyncio.wait_for(
                            sub.queue.get(), timeout=SSE_KEEPALIVE_SECONDS
                        )
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
            finally:
                # Runs on disconnect and on server shutdown, which is what
                # keeps the subscriber list from growing with every page
                # reload.
                body.state.unsubscribe(sub)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                # Nginx and friends buffer proxied responses by default,
                # which would hold every event until the stream ended.
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/stream.mjpg")
    async def stream_mjpg() -> StreamingResponse:
        async def frames():
            last: bytes | None = None
            while True:
                jpeg = body.latest_jpeg()
                if jpeg is not None and jpeg is not last:
                    last = jpeg
                    yield (
                        b"--"
                        + MJPEG_BOUNDARY.encode()
                        + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(jpeg)).encode()
                        + b"\r\n\r\n"
                        + jpeg
                        + b"\r\n"
                    )
                await asyncio.sleep(FRAME_POLL_SECONDS)

        return StreamingResponse(frames(), media_type=MJPEG_CONTENT_TYPE)

    @app.post("/ask")
    async def ask(
        audio: bytes = Body(default=b"", media_type="audio/wav"),
    ) -> JSONResponse:
        if not audio:
            return JSONResponse(
                {"error": "the request body was empty — no audio to transcribe"},
                status_code=400,
            )

        try:
            # Off the event loop. Transcription and the Gemini call are
            # both blocking network I/O: run inline they would freeze
            # every other request, including the MJPEG stream, for the
            # several seconds an answer takes.
            result = await run_in_threadpool(body.answer, audio)
        except Exception as exc:
            # Reported to the page rather than swallowed: the user pressed
            # a button and is owed an explanation. The traceback still
            # goes to the console for whoever is debugging.
            traceback.print_exc()
            return JSONResponse(
                {"error": f"{type(exc).__name__}: {exc}"}, status_code=502
            )

        return JSONResponse(result)

    @app.post("/exchange/begin")
    async def exchange_begin() -> JSONResponse:
        body.begin_exchange()
        return JSONResponse({"active": True})

    @app.post("/exchange/end")
    async def exchange_end() -> JSONResponse:
        body.end_exchange()
        return JSONResponse({"active": False})

    # Mounted LAST on purpose. Starlette matches routes in the order they
    # were added, and a mount at "/" matches everything — added earlier it
    # would swallow all six routes above.
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

    return app


def main() -> None:
    import uvicorn

    from bodies.web.body import WebBody

    body = WebBody()
    body.start()

    print(
        f"\nAURA web body listening on http://{HOST}:{PORT}\n"
        "Open that in your browser. Ctrl+C to stop."
    )

    try:
        uvicorn.run(create_app(body), host=HOST, port=PORT, log_level="warning")
    finally:
        body.stop()


if __name__ == "__main__":
    main()
