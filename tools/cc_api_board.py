"""CC API — Board 模块：实时教学白板 + SVG Canvas。"""
import asyncio
import json
import logging
import pathlib

from aiohttp import web

log = logging.getLogger("cc-api.board")

# ===== Slide mode =====

_steps: list[str] = []
_highlight_step: int = -1
_clients: set[web.WebSocketResponse] = set()
_title = "Jarvis Whiteboard"
_subtitle = ""
_state_ts: int = 0


def _bump_ts():
    global _state_ts
    import time
    _state_ts = int(time.time() * 1000)


def _build_state() -> dict:
    return {"type": "full_state", "title": _title, "subtitle": _subtitle,
            "steps": _steps, "highlight": _highlight_step, "ts": _state_ts}


async def _broadcast(msg: dict):
    data = json.dumps(msg, ensure_ascii=False)
    dead = set()
    for ws in _clients:
        try:
            await ws.send_str(data)
        except Exception:
            dead.add(ws)
    _clients.difference_update(dead)


async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    _clients.add(ws)
    log.info("Client connected (%d total)", len(_clients))
    await ws.send_str(json.dumps(_build_state(), ensure_ascii=False))
    try:
        async for _ in ws:
            pass
    finally:
        _clients.discard(ws)
        log.info("Client disconnected (%d left)", len(_clients))
    return ws


async def api_add(request):
    global _highlight_step
    body = await request.json()
    html = body.get("html", "")
    if not html:
        return web.json_response({"error": "missing html"}, status=400)
    _steps.append(html)
    if body.get("show", False):
        _highlight_step = len(_steps) - 1
    _bump_ts()
    await _broadcast({"type": "add_step", "index": len(_steps) - 1, "html": html, "highlight": _highlight_step})
    log.info("Added step %d (%d chars)", len(_steps) - 1, len(html))
    return web.json_response({"ok": True, "step": len(_steps) - 1})


async def api_update(request):
    body = await request.json()
    idx = body.get("step", -1)
    html = body.get("html", "")
    if idx < 0 or idx >= len(_steps):
        return web.json_response({"error": "invalid step"}, status=400)
    _steps[idx] = html
    _bump_ts()
    await _broadcast({"type": "update_step", "index": idx, "html": html})
    return web.json_response({"ok": True})


async def api_highlight(request):
    global _highlight_step
    body = await request.json()
    _highlight_step = body.get("step", -1)
    _bump_ts()
    await _broadcast({"type": "highlight", "step": _highlight_step})
    return web.json_response({"ok": True})


async def api_clear(request):
    global _highlight_step
    _steps.clear()
    _highlight_step = -1
    _bump_ts()
    await _broadcast({"type": "clear"})
    return web.json_response({"ok": True})


async def api_title(request):
    global _title, _subtitle
    body = await request.json()
    _title = body.get("title", _title)
    _subtitle = body.get("subtitle", _subtitle)
    _bump_ts()
    await _broadcast({"type": "title", "title": _title, "subtitle": _subtitle})
    return web.json_response({"ok": True})


async def api_status(request):
    return web.json_response({"steps": len(_steps), "clients": len(_clients),
                              "highlight": _highlight_step, "title": _title})


async def api_state(request):
    return web.json_response(_build_state())


# ===== Canvas mode (SVG whiteboard) =====

_canvas_elements: list[dict] = []
_canvas_clients: set[web.WebSocketResponse] = set()
_canvas_title = "Jarvis Canvas"
_canvas_subtitle = ""
_canvas_ts: int = 0


def _bump_canvas_ts():
    global _canvas_ts
    import time
    _canvas_ts = int(time.time() * 1000)


def _build_canvas_state() -> dict:
    return {"cmd": "full_state", "title": _canvas_title, "subtitle": _canvas_subtitle,
            "elements": _canvas_elements, "ts": _canvas_ts}


async def _canvas_broadcast(msg: dict):
    data = json.dumps(msg, ensure_ascii=False)
    dead = set()
    for ws in _canvas_clients:
        try:
            await ws.send_str(data)
        except Exception:
            dead.add(ws)
    _canvas_clients.difference_update(dead)


async def canvas_ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    _canvas_clients.add(ws)
    log.info("Canvas client connected (%d total)", len(_canvas_clients))
    await ws.send_str(json.dumps(_build_canvas_state(), ensure_ascii=False))
    try:
        async for _ in ws:
            pass
    finally:
        _canvas_clients.discard(ws)
        log.info("Canvas client disconnected (%d left)", len(_canvas_clients))
    return ws


async def canvas_draw(request):
    global _canvas_title, _canvas_subtitle
    body = await request.json()
    cmd = body.get("cmd", "")
    if not cmd:
        return web.json_response({"error": "missing cmd"}, status=400)
    if cmd == "clear":
        _canvas_elements.clear()
        _bump_canvas_ts()
        await _canvas_broadcast({"cmd": "clear"})
        return web.json_response({"ok": True})
    if cmd == "remove":
        eid = body.get("id", "")
        _canvas_elements[:] = [e for e in _canvas_elements if e.get("id") != eid]
        _bump_canvas_ts()
        await _canvas_broadcast(body)
        return web.json_response({"ok": True})
    if cmd == "highlight":
        _bump_canvas_ts()
        await _canvas_broadcast(body)
        return web.json_response({"ok": True})
    if cmd == "title":
        _canvas_title = body.get("title", _canvas_title)
        _canvas_subtitle = body.get("subtitle", _canvas_subtitle)
        _bump_canvas_ts()
        await _canvas_broadcast(body)
        return web.json_response({"ok": True})
    _canvas_elements.append(body)
    _bump_canvas_ts()
    await _canvas_broadcast(body)
    log.info("Canvas draw: %s id=%s", cmd, body.get("id", "?"))
    return web.json_response({"ok": True, "elements": len(_canvas_elements)})


async def canvas_batch(request):
    global _canvas_title, _canvas_subtitle
    body = await request.json()
    for cmd_body in body.get("commands", []):
        cmd = cmd_body.get("cmd", "")
        if cmd == "clear":
            _canvas_elements.clear()
        elif cmd == "remove":
            _canvas_elements[:] = [e for e in _canvas_elements if e.get("id") != cmd_body.get("id", "")]
        elif cmd == "title":
            _canvas_title = cmd_body.get("title", _canvas_title)
            _canvas_subtitle = cmd_body.get("subtitle", _canvas_subtitle)
        elif cmd != "highlight":
            _canvas_elements.append(cmd_body)
        await _canvas_broadcast(cmd_body)
    _bump_canvas_ts()
    return web.json_response({"ok": True, "elements": len(_canvas_elements)})


async def canvas_state(request):
    return web.json_response(_build_canvas_state())


# ===== HTML pages =====

_TOOLS_DIR = pathlib.Path(__file__).parent
PAGE_HTML = (_TOOLS_DIR / "board-page.html").read_text()
CANVAS_HTML = (_TOOLS_DIR / "board-canvas.html").read_text()


async def page_handler(request):
    return web.Response(text=PAGE_HTML, content_type="text/html")


async def canvas_page_handler(request):
    return web.Response(text=CANVAS_HTML, content_type="text/html")


# ===== Route registration =====

def register_routes(app: web.Application):
    # Slide mode
    app.router.add_get("/board", page_handler)
    app.router.add_get("/board/", page_handler)
    app.router.add_get("/board/ws", ws_handler)
    app.router.add_post("/board/api/add", api_add)
    app.router.add_post("/board/api/update", api_update)
    app.router.add_post("/board/api/highlight", api_highlight)
    app.router.add_post("/board/api/clear", api_clear)
    app.router.add_post("/board/api/title", api_title)
    app.router.add_get("/board/api/status", api_status)
    app.router.add_get("/board/api/state", api_state)
    # Canvas mode
    app.router.add_get("/canvas", canvas_page_handler)
    app.router.add_get("/canvas/", canvas_page_handler)
    app.router.add_get("/canvas/ws", canvas_ws_handler)
    app.router.add_post("/canvas/api/draw", canvas_draw)
    app.router.add_post("/canvas/api/batch", canvas_batch)
    app.router.add_get("/canvas/api/state", canvas_state)
    app.router.add_get("/board/canvas", canvas_page_handler)
    app.router.add_get("/board/canvas/", canvas_page_handler)
    app.router.add_get("/board/canvas/ws", canvas_ws_handler)
    app.router.add_post("/board/canvas/api/draw", canvas_draw)
    app.router.add_post("/board/canvas/api/batch", canvas_batch)
    app.router.add_get("/board/canvas/api/state", canvas_state)
    # 兼容本地访问
    app.router.add_get("/", page_handler)
    app.router.add_get("/ws", ws_handler)
    app.router.add_post("/api/add", api_add)
    app.router.add_post("/api/update", api_update)
    app.router.add_post("/api/highlight", api_highlight)
    app.router.add_post("/api/clear", api_clear)
    app.router.add_post("/api/title", api_title)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/draw", canvas_draw)
    app.router.add_post("/api/batch", canvas_batch)
    app.router.add_get("/api/canvas/state", canvas_state)
