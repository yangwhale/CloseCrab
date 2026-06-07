"""CC API — Feishu 模块：以 Chris 身份发飞书消息。"""
import asyncio
import logging
import os
import pathlib

from aiohttp import web

log = logging.getLogger("cc-api.feishu")

_API_KEY = os.environ.get("FEISHU_API_KEY", "UIxgVkm4v0sMv89_7YEUa5zUwC_DYb9pH_R5meXE4cI")
_SEND_SCRIPT = str(pathlib.Path(__file__).parent.parent / "skills" / "feishu-user-msg" / "scripts" / "send_as_user.py")


async def api_send(request):
    """POST /feishu/send — 以 Chris 身份发飞书消息到 Jarvis。
    Body: {"text": "消息内容", "key": "API密钥"}
    """
    body = await request.json()
    key = body.get("key", request.headers.get("X-API-Key", ""))
    if key != _API_KEY:
        return web.json_response({"error": "unauthorized"}, status=401)
    text = body.get("text", "").strip()
    if not text:
        return web.json_response({"error": "missing text"}, status=400)
    try:
        proc = await asyncio.create_subprocess_exec(
            "python3", _SEND_SCRIPT, "--to", "jarvis", "--text", text,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        ok = proc.returncode == 0
        log.info("Feishu send: %s → rc=%d", text[:60], proc.returncode)
        return web.json_response({"ok": ok, "text": text,
                                  "detail": (stdout or stderr or b"").decode()[:200]})
    except Exception as e:
        log.error("Feishu send error: %s", e)
        return web.json_response({"error": str(e)}, status=500)


def register_routes(app: web.Application):
    app.router.add_post("/feishu/send", api_send)
    app.router.add_post("/feishu/api/send", api_send)
