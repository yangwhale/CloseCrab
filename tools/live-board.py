#!/usr/bin/env python3
"""Jarvis Live Board — 实时教学白板服务器。

WebSocket 推送，Jarvis 通过 HTTP API 控制页面内容。
浏览器端自动重连，内容实时更新。

用法:
    python3 tools/live-board.py              # 启动服务 (port 8765)
    curl localhost:8765/api/add -d '{"html":"<h2>Hello</h2>"}'
    curl localhost:8765/api/clear
    curl localhost:8765/api/highlight -d '{"step":1}'
"""

import asyncio
import json
import logging
from aiohttp import web

log = logging.getLogger("live-board")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

# 全局状态
_steps: list[str] = []
_highlight_step: int = -1
_clients: set[web.WebSocketResponse] = set()
_title = "Jarvis Whiteboard"
_subtitle = ""


def _build_state() -> dict:
    return {
        "type": "full_state",
        "title": _title,
        "subtitle": _subtitle,
        "steps": _steps,
        "highlight": _highlight_step,
    }


async def _broadcast(msg: dict):
    global _clients
    data = json.dumps(msg, ensure_ascii=False)
    dead = set()
    for ws in _clients:
        try:
            await ws.send_str(data)
        except Exception:
            dead.add(ws)
    _clients -= dead


# --- WebSocket endpoint ---

async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    _clients.add(ws)
    log.info("Client connected (%d total)", len(_clients))
    # 发送当前完整状态
    await ws.send_str(json.dumps(_build_state(), ensure_ascii=False))
    try:
        async for msg in ws:
            pass  # 客户端不需要发消息
    finally:
        _clients.discard(ws)
        log.info("Client disconnected (%d left)", len(_clients))
    return ws


# --- HTTP API ---

async def api_add(request):
    """添加一个步骤。POST body: {"html": "<p>内容</p>"}"""
    global _highlight_step
    body = await request.json()
    html = body.get("html", "")
    if not html:
        return web.json_response({"error": "missing html"}, status=400)
    _steps.append(html)
    _highlight_step = len(_steps) - 1
    await _broadcast({
        "type": "add_step",
        "index": len(_steps) - 1,
        "html": html,
        "highlight": _highlight_step,
    })
    log.info("Added step %d (%d chars)", len(_steps) - 1, len(html))
    return web.json_response({"ok": True, "step": len(_steps) - 1})


async def api_update(request):
    """更新指定步骤。POST body: {"step": 0, "html": "..."}"""
    body = await request.json()
    idx = body.get("step", -1)
    html = body.get("html", "")
    if idx < 0 or idx >= len(_steps):
        return web.json_response({"error": "invalid step"}, status=400)
    _steps[idx] = html
    await _broadcast({"type": "update_step", "index": idx, "html": html})
    return web.json_response({"ok": True})


async def api_highlight(request):
    """高亮指定步骤。POST body: {"step": 1}"""
    global _highlight_step
    body = await request.json()
    _highlight_step = body.get("step", -1)
    await _broadcast({"type": "highlight", "step": _highlight_step})
    return web.json_response({"ok": True})


async def api_clear(request):
    """清空白板。"""
    global _highlight_step
    _steps.clear()
    _highlight_step = -1
    await _broadcast({"type": "clear"})
    return web.json_response({"ok": True})


async def api_title(request):
    """设置标题。POST body: {"title": "...", "subtitle": "..."}"""
    global _title, _subtitle
    body = await request.json()
    _title = body.get("title", _title)
    _subtitle = body.get("subtitle", _subtitle)
    await _broadcast({"type": "title", "title": _title, "subtitle": _subtitle})
    return web.json_response({"ok": True})


async def api_status(request):
    """查看当前状态。"""
    return web.json_response({
        "steps": len(_steps),
        "clients": len(_clients),
        "highlight": _highlight_step,
        "title": _title,
    })


# --- 前端页面 ---

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Jarvis Whiteboard</title>
<style>
  :root { --bg: #fafafa; --card: #fff; --accent: #1a73e8; --text: #1a1a1a; --dim: #666; --hl: #e8f0fe; --border: #e0e0e0; }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'Google Sans', system-ui, sans-serif; background: var(--bg); color: var(--text); }
  .board { max-width: 850px; margin: 2rem auto; background: var(--card); border-radius: 16px;
           box-shadow: 0 2px 16px rgba(0,0,0,0.07); padding: 2.5rem; min-height: 85vh;
           position: relative; overflow: hidden; }
  .board::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 4px; background: var(--accent); border-radius: 16px 16px 0 0; }
  .header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 2rem; padding-bottom: 1rem; border-bottom: 2px solid var(--border); }
  .header h1 { font-size: 1.2rem; font-weight: 500; color: var(--dim); }
  .header .sub { font-size: 0.85rem; color: var(--accent); }
  .conn { display: flex; align-items: center; gap: 6px; font-size: 0.75rem; }
  .conn-dot { width: 8px; height: 8px; border-radius: 50%; }
  .conn-dot.on { background: #34a853; animation: pulse 2s infinite; }
  .conn-dot.off { background: #ea4335; }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.3; } }
  #steps { }
  .step { padding: 1.2rem 1.5rem; margin-bottom: 1rem; border-radius: 10px; border: 2px solid transparent;
          transition: all 0.3s ease; animation: slideIn 0.4s ease-out; }
  .step.active { border-color: var(--accent); background: var(--hl); }
  @keyframes slideIn { from { opacity: 0; transform: translateX(-20px); } to { opacity: 1; transform: translateX(0); } }
  .step-num { display: inline-block; background: var(--accent); color: #fff; width: 24px; height: 24px;
              border-radius: 50%; text-align: center; line-height: 24px; font-size: 0.75rem; margin-right: 8px; vertical-align: middle; }
  .step h2 { font-size: 1.1rem; margin-bottom: 0.5rem; }
  .step p { line-height: 1.8; font-size: 0.95rem; }
  .step pre { background: #f6f8fa; border: 1px solid var(--border); border-radius: 8px; padding: 1rem;
              margin: 0.8rem 0; font-family: 'JetBrains Mono', monospace; font-size: 0.82rem;
              overflow-x: auto; line-height: 1.7; }
  .step .formula { text-align: center; font-size: 1.3rem; color: var(--accent); margin: 1rem 0; font-weight: 600; }
  .step .note { background: #f0f4ff; border-left: 3px solid var(--accent); padding: 0.6rem 1rem;
                border-radius: 0 6px 6px 0; font-size: 0.88rem; color: var(--dim); margin-top: 0.8rem; }
  .step .hl { background: #fff3cd; padding: 1px 5px; border-radius: 3px; }
  .empty { text-align: center; color: #ccc; padding: 4rem 0; font-size: 1rem; }
  .footer { text-align: center; color: #ccc; font-size: 0.72rem; margin-top: 2rem; }
</style>
</head>
<body>
<div class="board">
  <div class="header">
    <div>
      <h1 id="title">Jarvis Whiteboard</h1>
      <div class="sub" id="subtitle"></div>
    </div>
    <div class="conn">
      <div class="conn-dot off" id="conn-dot"></div>
      <span id="conn-text">连接中...</span>
    </div>
  </div>
  <div id="steps">
    <div class="empty" id="empty-hint">等待 Jarvis 开始书写...</div>
  </div>
  <div class="footer">Jarvis Live Board · 实时同步</div>
</div>

<script>
const stepsEl = document.getElementById('steps');
const emptyEl = document.getElementById('empty-hint');
const dotEl = document.getElementById('conn-dot');
const connText = document.getElementById('conn-text');
const titleEl = document.getElementById('title');
const subEl = document.getElementById('subtitle');
let ws;

function connect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const basePath = location.pathname.replace(/\/$/, '') || '';
  ws = new WebSocket(proto + '//' + location.host + basePath + '/ws');
  ws.onopen = () => { dotEl.className = 'conn-dot on'; connText.textContent = '已连接'; };
  ws.onclose = () => { dotEl.className = 'conn-dot off'; connText.textContent = '断线重连...'; setTimeout(connect, 2000); };
  ws.onerror = () => ws.close();
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'full_state') {
      titleEl.textContent = msg.title || 'Jarvis Whiteboard';
      subEl.textContent = msg.subtitle || '';
      stepsEl.innerHTML = '';
      if (msg.steps.length === 0) {
        stepsEl.innerHTML = '<div class="empty" id="empty-hint">等待 Jarvis 开始书写...</div>';
      } else {
        msg.steps.forEach((html, i) => addStepDOM(i, html, i === msg.highlight));
      }
    } else if (msg.type === 'add_step') {
      const emp = document.getElementById('empty-hint');
      if (emp) emp.remove();
      clearHighlights();
      addStepDOM(msg.index, msg.html, true);
      scrollToBottom();
    } else if (msg.type === 'update_step') {
      const el = document.getElementById('step-' + msg.index);
      if (el) el.querySelector('.step-body').innerHTML = msg.html;
    } else if (msg.type === 'highlight') {
      clearHighlights();
      const el = document.getElementById('step-' + msg.step);
      if (el) { el.classList.add('active'); el.scrollIntoView({behavior:'smooth', block:'center'}); }
    } else if (msg.type === 'clear') {
      stepsEl.innerHTML = '<div class="empty" id="empty-hint">等待 Jarvis 开始书写...</div>';
    } else if (msg.type === 'title') {
      titleEl.textContent = msg.title;
      subEl.textContent = msg.subtitle || '';
    }
  };
}

function addStepDOM(idx, html, active) {
  const div = document.createElement('div');
  div.id = 'step-' + idx;
  div.className = 'step' + (active ? ' active' : '');
  div.innerHTML = '<span class="step-num">' + (idx+1) + '</span><div class="step-body" style="display:inline">' + html + '</div>';
  stepsEl.appendChild(div);
}
function clearHighlights() { document.querySelectorAll('.step.active').forEach(e => e.classList.remove('active')); }
function scrollToBottom() { setTimeout(() => window.scrollTo({top: document.body.scrollHeight, behavior:'smooth'}), 100); }

connect();
</script>
</body>
</html>
"""


async def page_handler(request):
    return web.Response(text=PAGE_HTML, content_type="text/html")


# --- App ---

def create_app():
    app = web.Application()
    app.router.add_get("/board", page_handler)
    app.router.add_get("/board/", page_handler)
    app.router.add_get("/board/ws", ws_handler)
    app.router.add_post("/board/api/add", api_add)
    app.router.add_post("/board/api/update", api_update)
    app.router.add_post("/board/api/highlight", api_highlight)
    app.router.add_post("/board/api/clear", api_clear)
    app.router.add_post("/board/api/title", api_title)
    app.router.add_get("/board/api/status", api_status)
    # 兼容不带 /board 前缀的本地访问
    app.router.add_get("/", page_handler)
    app.router.add_get("/ws", ws_handler)
    app.router.add_post("/api/add", api_add)
    app.router.add_post("/api/update", api_update)
    app.router.add_post("/api/highlight", api_highlight)
    app.router.add_post("/api/clear", api_clear)
    app.router.add_post("/api/title", api_title)
    app.router.add_get("/api/status", api_status)
    return app


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="0.0.0.0")
    args = p.parse_args()
    app = create_app()
    log.info("Live Board starting on %s:%d", args.host, args.port)
    web.run_app(app, host=args.host, port=args.port, print=None)
