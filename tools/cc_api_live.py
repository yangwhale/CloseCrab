"""CC API — Live 模块：HLS 语音直播流 + SSE 事件推送。

路由:
  GET /live/{filename}        → HLS playlist / segments
  GET /assets/live/{filename} → 同上 (公开路径)
  GET /live/events            → SSE 事件流 (speaking_started 等)
  GET /assets/live/events     → 同上

FFmpeg 产出到 /tmp/hls-live/，此模块直接 serve 文件并设正确 MIME type。
"""
import asyncio
import logging
import os
import time
from aiohttp import web

log = logging.getLogger("cc-api.live")

_sse_clients: list[asyncio.Queue] = []
_ws_clients: set[web.WebSocketResponse] = set()

_HLS_DIR = "/tmp/hls-live"

_MIME = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/MP2T",
    ".m4s": "video/iso.segment",
    ".mp4": "video/mp4",
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}


_STATIC_DIR = os.path.expanduser("~/cc-pages-new/assets/live")


async def _serve_hls(request: web.Request) -> web.Response:
    filename = request.match_info["filename"]
    if ".." in filename or "/" in filename:
        raise web.HTTPForbidden()
    path = os.path.join(_HLS_DIR, filename)
    if not os.path.exists(path):
        path = os.path.join(_STATIC_DIR, filename)
    if not os.path.exists(path):
        raise web.HTTPNotFound()
    ext = os.path.splitext(filename)[1].lower()
    content_type = _MIME.get(ext, "application/octet-stream")
    return web.FileResponse(
        path,
        headers={
            "Content-Type": content_type,
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "no-cache",
        },
    )


async def _sse_handler(request: web.Request) -> web.StreamResponse:
    resp = web.StreamResponse()
    resp.headers["Content-Type"] = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    await resp.prepare(request)

    q: asyncio.Queue = asyncio.Queue()
    _sse_clients.append(q)
    try:
        await resp.write(b"data: connected\n\n")
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=20)
                await resp.write(f"data: {msg}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": keepalive\n\n")
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        _sse_clients.remove(q)
    return resp


def notify_speaking():
    for q in _sse_clients:
        try:
            q.put_nowait("speaking_started")
        except asyncio.QueueFull:
            pass


_current_state = {"event": "idle", "url": "", "ts": 0}


async def _ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    _ws_clients.add(ws)
    log.info("Live WS client connected (%d total)", len(_ws_clients))
    import json
    await ws.send_str(json.dumps(_current_state))
    try:
        async for _ in ws:
            pass
    finally:
        _ws_clients.discard(ws)
        log.info("Live WS client disconnected (%d left)", len(_ws_clients))
    return ws


async def _ws_broadcast(msg: dict):
    import json
    data = json.dumps(msg)
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_str(data)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)


async def _notify_handler(request: web.Request) -> web.Response:
    import json
    event = request.query.get("event", "start")
    url = request.query.get("url", "")
    _current_state["event"] = "speaking" if event == "start" else "idle"
    _current_state["url"] = url if event == "start" else _current_state.get("url", "")
    _current_state["ts"] = time.time()
    await _ws_broadcast(_current_state)
    for q in _sse_clients:
        try:
            q.put_nowait(json.dumps(_current_state))
        except asyncio.QueueFull:
            pass
    return web.Response(text="ok")


async def _status_handler(request: web.Request) -> web.Response:
    import json
    return web.Response(
        text=json.dumps(_current_state),
        content_type="application/json",
        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"},
    )


def register_routes(app: web.Application):
    app.router.add_get("/live/ws", _ws_handler)
    app.router.add_get("/assets/live/ws", _ws_handler)
    app.router.add_get("/live/events", _sse_handler)
    app.router.add_get("/assets/live/events", _sse_handler)
    app.router.add_get("/live/notify", _notify_handler)
    app.router.add_get("/live/status", _status_handler)
    app.router.add_get("/assets/live/status", _status_handler)
    app.router.add_get("/live/{filename}", _serve_hls)
    app.router.add_get("/assets/live/{filename}", _serve_hls)
    log.info("Live HLS routes registered: /live/ + /assets/live/ + SSE events")
