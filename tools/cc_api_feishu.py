"""CC API — Feishu 模块：以 Chris 身份发飞书消息（文字 + 语音）。"""
import asyncio
import logging
import os
import pathlib
import tempfile

from aiohttp import web

log = logging.getLogger("cc-api.feishu")

_API_KEY = os.environ.get("FEISHU_API_KEY", "UIxgVkm4v0sMv89_7YEUa5zUwC_DYb9pH_R5meXE4cI")
_SEND_SCRIPT = str(pathlib.Path(__file__).parent.parent / "skills" / "feishu-user-msg" / "scripts" / "send_as_user.py")


def _check_key(request, body=None):
    if body:
        key = body.get("key", request.headers.get("X-API-Key", ""))
    else:
        key = request.headers.get("X-API-Key", request.query.get("key", ""))
    return key == _API_KEY


async def api_send(request):
    """POST /feishu/send — 以 Chris 身份发文字消息到 Jarvis。
    Body: {"text": "消息内容", "key": "API密钥"}
    """
    body = await request.json()
    if not _check_key(request, body):
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
        log.info("Feishu send text: %s → rc=%d", text[:60], proc.returncode)
        return web.json_response({"ok": ok, "text": text,
                                  "detail": (stdout or stderr or b"").decode()[:200]})
    except Exception as e:
        log.error("Feishu send error: %s", e)
        return web.json_response({"error": str(e)}, status=500)


async def api_send_audio(request):
    """POST /feishu/send_audio — 以 Chris 身份发语音消息到 Jarvis。
    multipart/form-data: file=<音频文件>, key=<API密钥>
    """
    reader = await request.multipart()
    audio_path = None
    key = request.headers.get("X-API-Key", request.query.get("key", ""))
    try:
        async for part in reader:
            if part.name == "key":
                key = (await part.text()).strip()
            elif part.name == "file":
                suffix = ".ogg"
                fname = part.filename or ""
                if fname.endswith(".m4a"):
                    suffix = ".m4a"
                elif fname.endswith(".wav"):
                    suffix = ".wav"
                elif fname.endswith(".mp3"):
                    suffix = ".mp3"
                fd, audio_path = tempfile.mkstemp(suffix=suffix, prefix="feishu-audio-")
                with os.fdopen(fd, "wb") as f:
                    while True:
                        chunk = await part.read_chunk(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                log.info("Received audio: %s (%d bytes)", audio_path, os.path.getsize(audio_path))

        if key != _API_KEY:
            return web.json_response({"error": "unauthorized"}, status=401)
        if not audio_path:
            return web.json_response({"error": "missing audio file"}, status=400)

        proc = await asyncio.create_subprocess_exec(
            "python3", _SEND_SCRIPT, "--to", "jarvis", "--audio", audio_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        ok = proc.returncode == 0
        log.info("Feishu send audio: %s → rc=%d", audio_path, proc.returncode)
        return web.json_response({"ok": ok, "audio": os.path.basename(audio_path),
                                  "detail": (stdout or stderr or b"").decode()[:200]})
    except Exception as e:
        log.error("Feishu send audio error: %s", e)
        return web.json_response({"error": str(e)}, status=500)
    finally:
        if audio_path and os.path.exists(audio_path):
            os.unlink(audio_path)


def register_routes(app: web.Application):
    app.router.add_post("/feishu/send", api_send)
    app.router.add_post("/feishu/api/send", api_send)
    app.router.add_post("/feishu/send_audio", api_send_audio)
    app.router.add_post("/feishu/api/send_audio", api_send_audio)
