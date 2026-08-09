#!/usr/bin/env python3
"""Generate a multi-lesson Chinese-dictation HTML page.

Each "lesson" becomes a tab. Each word in a lesson becomes a small OGG file.
The HTML scheduler plays them client-side with adjustable interval.

URL is stable per `slug` — re-running with the same slug **updates in place**
(the user's bookmark always points to the latest version). Audio files are
hash-named, so re-runs that don't change a word's hanzi/voice/engine **skip TTS
entirely** for that word — perfect for incremental "add this lesson's words" flow.

Input JSON shape:

    {
      "title": "中文聽寫合集",
      "slug": "exam-2026-spring",
      "pause_seconds": 5,
      "repeat": 2,
      "engine": "gemini",
      "voice": "erinome",
      "lessons": [
        {"id": "L01", "title": "第一冊", "words": []},
        {"id": "L02", "title": "單元二・第5課", "words": [
          {"hanzi": "面子", "pinyin": "miàn zi"}, ...
        ]},
        ...
      ]
    }

Output: prints the public CC Pages URL on stdout.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

TTS_SCRIPT = Path.home() / ".claude/skills/tts-generator/scripts/tts-generate.py"
WEB_ROOT = Path(os.environ.get("CC_PAGES_WEB_ROOT", "/gcs/cc-pages"))
URL_PREFIX = os.environ.get("CC_PAGES_URL_PREFIX", "https://www.closecrab.com").rstrip("/")

# Bumping this invalidates all cached audio (when prompt text changes meaningfully)
PROMPT_VERSION = "v2-cctv-brisk"


def slugify(text: str | None, max_len: int = 60) -> str:
    if not text:
        return ""
    text = re.sub(r"\s+", "-", text.strip())
    text = re.sub(r"[^A-Za-z0-9_-]", "", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:max_len]


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(
            f"command failed ({res.returncode}): {' '.join(cmd)}\nstderr:\n{res.stderr}"
        )
    return res


def word_hash(hanzi: str, voice: str, engine: str, repeat: int) -> str:
    """Stable short hash — same word/voice/engine/repeat → same filename."""
    key = f"{PROMPT_VERSION}|{engine}|{voice}|{repeat}|{hanzi}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def tts_word(hanzi: str, repeat: int, voice: str, engine: str) -> Path:
    """Generate TTS audio for one word, read `repeat` times.

    For Gemini, prepend a director tag that gets compiled into
    "Say the following in Chinese, ..." — forces Mandarin mode so 繁體
    characters (會, 實, 機, 預, 學, ...) aren't mis-read as Japanese kanji.
    """
    transcript = ("。".join([hanzi] * repeat)) + "。"
    if engine == "gemini":
        text = (
            "[clearly, like a Chinese CCTV news anchor dictating each word "
            "for elementary school students, standard Mandarin pronunciation, "
            "precise tones, at a clear and slightly brisk pace, "
            "with a brief pause between repetitions] "
            + transcript
        )
    else:
        text = transcript
    res = run([
        "python3", str(TTS_SCRIPT),
        "--engine", engine,
        "--voice", voice,
        text,
    ])
    path = Path(res.stdout.strip().splitlines()[-1])
    if not path.exists():
        raise RuntimeError(f"TTS returned non-existent path: {path!r}")
    return path


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · 聽寫</title>
<meta property="og:title" content="{title} · 中文聽寫">
<meta property="og:description" content="{n_lessons} 課 · {n_total_words} 詞 · 客戶端可調間隔">
<meta name="theme-color" content="#22C55E">
<style>
  :root {{
    --bg: #0f172a;
    --card: #1e293b;
    --card-hover: #334155;
    --card-active: #15803d;
    --text: #e2e8f0;
    --muted: #94a3b8;
    --accent: #22c55e;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 20px 14px 60px;
    font-family: -apple-system, "PingFang TC", "PingFang SC", "Microsoft YaHei",
                 "Noto Sans CJK TC", "Noto Sans CJK SC", sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.5;
    max-width: 920px; margin-left: auto; margin-right: auto;
  }}
  header {{ text-align: center; margin-bottom: 14px; }}
  h1 {{ margin: 0 0 6px; font-size: 22px; }}
  .meta {{ color: var(--muted); font-size: 12px; }}

  /* Tabs */
  .tabs {{
    display: flex; gap: 6px; overflow-x: auto;
    padding: 4px 0 12px; margin-bottom: 12px;
    scrollbar-width: thin;
    border-bottom: 1px solid var(--card);
  }}
  .tab {{
    flex: 0 0 auto;
    background: transparent; color: var(--muted); border: none;
    padding: 8px 14px; cursor: pointer; font-size: 13px;
    border-radius: 8px 8px 0 0;
    border-bottom: 3px solid transparent;
    transition: all 0.15s;
    white-space: nowrap;
  }}
  .tab:hover {{ color: var(--text); background: var(--card); }}
  .tab.active {{
    color: var(--accent); border-bottom-color: var(--accent);
    background: var(--card);
    font-weight: 600;
  }}
  .tab .count {{
    display: inline-block; margin-left: 6px;
    padding: 1px 7px; border-radius: 999px;
    background: rgba(148, 163, 184, 0.2); color: var(--muted);
    font-size: 11px;
  }}
  .tab.active .count {{
    background: rgba(34, 197, 94, 0.25); color: var(--accent);
  }}

  .controls {{
    background: var(--card); border-radius: 14px; padding: 12px 16px;
    margin-bottom: 16px;
    display: flex; gap: 12px; align-items: center; flex-wrap: wrap;
    box-shadow: 0 4px 14px rgba(0,0,0,0.25);
  }}
  .controls label {{ font-size: 13px; color: var(--muted); }}
  .controls input[type=number] {{
    width: 52px; padding: 5px 7px; font-size: 14px;
    background: var(--bg); color: var(--text);
    border: 1px solid var(--card-hover); border-radius: 8px;
    text-align: center;
  }}
  button {{
    background: var(--card-hover); color: var(--text); border: none;
    border-radius: 999px; padding: 7px 14px; cursor: pointer; font-size: 13px;
    transition: background 0.15s, transform 0.05s;
  }}
  button:hover {{ background: var(--accent); color: #052e10; }}
  button:active {{ transform: scale(0.96); }}
  button:disabled {{ opacity: 0.4; cursor: not-allowed; }}
  button.primary {{ background: var(--accent); color: #052e10; font-weight: 600; }}
  .status {{
    margin-left: auto; font-size: 12px; color: var(--muted);
  }}
  .status b {{ color: var(--text); font-weight: 600; }}

  .toolbar {{
    display: flex; justify-content: center; gap: 8px; margin-bottom: 14px;
    flex-wrap: wrap;
  }}

  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
    gap: 10px;
  }}
  .card {{
    background: var(--card); border-radius: 10px; padding: 12px 8px 14px;
    text-align: center; cursor: pointer;
    transition: background 0.15s, transform 0.1s, box-shadow 0.15s;
    user-select: none; position: relative;
    border: 2px solid transparent;
  }}
  .card:hover {{ background: var(--card-hover); }}
  .card:active {{ transform: scale(0.97); }}
  .card.now {{
    border-color: var(--accent);
    background: var(--card-active);
    box-shadow: 0 0 0 4px rgba(34, 197, 94, 0.2);
  }}
  .idx {{
    position: absolute; top: 4px; left: 8px;
    color: var(--muted); font-size: 11px;
  }}
  .pinyin {{
    font-size: 12px; color: var(--muted); margin-bottom: 4px;
    letter-spacing: 0.3px;
  }}
  .hanzi {{
    font-size: 24px; font-weight: 500; letter-spacing: 3px;
  }}
  .card.hidden .pinyin, .card.hidden .hanzi {{
    filter: blur(7px);
    transition: filter 0.2s;
  }}
  .card.hidden::after {{
    content: "點擊顯示";
    position: absolute; inset: 0;
    display: flex; align-items: center; justify-content: center;
    color: var(--accent); font-size: 12px;
    background: rgba(15, 23, 42, 0.45);
    border-radius: 10px;
    pointer-events: none;
  }}
  .empty {{
    text-align: center; padding: 60px 20px; color: var(--muted);
    background: var(--card); border-radius: 14px;
    border: 2px dashed var(--card-hover);
  }}
  .empty p {{ margin: 4px 0; }}
  .empty .big {{ font-size: 36px; margin-bottom: 12px; }}

  @media print {{
    body {{ background: white; color: black; }}
    .controls, .toolbar, .tabs {{ display: none; }}
    .card {{ background: white; border: 1px solid #ccc; }}
    .card.hidden .pinyin, .card.hidden .hanzi {{ filter: none; }}
    .card.hidden::after {{ display: none; }}
    .pinyin {{ color: #666; }}
  }}
</style>
</head>
<body>

<header>
  <h1>{title}</h1>
  <div class="meta">{date} · {n_lessons} 課 · 共 {n_total_words} 詞 · 每詞讀 {repeat} 遍</div>
</header>

<div class="tabs" id="tabs"></div>

<div class="controls">
  <label>間隔 <input type="number" id="pause" value="{pause}" min="1" max="60" step="1"></label>
  <label style="color: var(--muted)">秒</label>
  <button id="btn-start" class="primary" onclick="start()">▶ 開始</button>
  <button id="btn-pause" onclick="pauseToggle()" disabled>⏸ 暫停</button>
  <button id="btn-next" onclick="skip()" disabled>⏭ 跳下一個</button>
  <button id="btn-stop" onclick="stop()" disabled>⏹ 停止</button>
  <button onclick="reset()">⟲ 重置</button>
  <div class="status">第 <b id="cur">—</b> / <b id="total">0</b> 詞</div>
</div>
<audio id="player" preload="auto"></audio>

<div class="toolbar">
  <button onclick="toggleAll(true)">全部顯示</button>
  <button onclick="toggleAll(false)">全部隱藏</button>
  <button onclick="window.print()">列印答案</button>
</div>

<div id="content"></div>

<script>
const LESSONS = {lessons_json};
const $ = (id) => document.getElementById(id);
const player = $('player');

let activeLessonId = LESSONS[0]?.id || null;
let idx = -1;
let state = 'idle';                 // idle | playing
let paused = false;
let sleepCanceler = null;
let audioResolver = null;
let runGen = 0;

player.addEventListener('ended', () => {{ if (audioResolver) audioResolver(); }});
player.addEventListener('error', () => {{ if (audioResolver) audioResolver(); }});

function lesson() {{ return LESSONS.find(l => l.id === activeLessonId); }}
function words() {{ return lesson()?.words || []; }}

function renderTabs() {{
  $('tabs').innerHTML = LESSONS.map(l =>
    `<button class="tab ${{l.id === activeLessonId ? 'active' : ''}}" onclick="selectLesson('${{l.id}}')">${{
      escapeHtml(l.title)
    }}<span class="count">${{l.words.length}}</span></button>`
  ).join('');
}}

function renderContent() {{
  const ws = words();
  if (ws.length === 0) {{
    $('content').innerHTML = `<div class="empty">
      <p class="big">📝</p>
      <p>這一課還沒有詞</p>
      <p style="font-size: 12px">截圖發給 athena 來補充</p>
    </div>`;
  }} else {{
    $('content').innerHTML = '<div class="grid">' + ws.map((w, i) =>
      `<div class="card hidden" onclick="toggleCard(this)">
        <span class="idx">${{i + 1}}</span>
        <div class="pinyin">${{escapeHtml(w.pinyin || '')}}</div>
        <div class="hanzi">${{escapeHtml(w.hanzi)}}</div>
      </div>`
    ).join('') + '</div>';
  }}
  $('total').textContent = ws.length;
}}

function escapeHtml(s) {{
  return String(s).replace(/[&<>"']/g, c =>
    ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[c]);
}}

function selectLesson(id) {{
  if (id === activeLessonId) return;
  stop();
  activeLessonId = id;
  idx = -1;
  updateStatus();
  renderTabs();
  renderContent();
}}

function toggleCard(el) {{ el.classList.toggle('hidden'); }}
function toggleAll(show) {{
  document.querySelectorAll('.card').forEach(c =>
    show ? c.classList.remove('hidden') : c.classList.add('hidden')
  );
}}

function highlight(i) {{
  document.querySelectorAll('.card.now').forEach(c => c.classList.remove('now'));
  if (i >= 0 && i < words().length) {{
    const card = document.querySelectorAll('.card')[i];
    if (!card) return;
    card.classList.add('now');
    card.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
  }}
}}
function reveal(i) {{
  const ws = words();
  if (i < 0 || i >= ws.length) return;
  const card = document.querySelectorAll('.card')[i];
  if (card) card.classList.remove('hidden');
}}
function updateStatus() {{
  $('cur').textContent = idx >= 0 ? (idx + 1) : '—';
}}
function setButtons(running) {{
  $('btn-start').disabled = running;
  $('btn-pause').disabled = !running;
  $('btn-next').disabled = !running;
  $('btn-stop').disabled = !running;
}}

function playAudio(url) {{
  return new Promise(resolve => {{
    audioResolver = () => {{ audioResolver = null; resolve(); }};
    player.src = url;
    player.load();
    player.play().catch(() => {{ if (audioResolver) audioResolver(); }});
  }});
}}
function sleepCancelable(ms) {{
  return new Promise(resolve => {{
    const t = setTimeout(() => {{ sleepCanceler = null; resolve(); }}, ms);
    sleepCanceler = () => {{ clearTimeout(t); sleepCanceler = null; resolve(); }};
  }});
}}
async function waitWhilePaused() {{
  while (paused && state !== 'stopped') {{
    await new Promise(r => setTimeout(r, 100));
  }}
}}
function getPauseMs() {{
  return Math.max(0, parseInt($('pause').value, 10) || 0) * 1000;
}}
function pauseForWord(hanzi) {{
  // 2-char baseline; 3-char ×1.5, 4-char ×2. Min 1× to protect 1-char words.
  const factor = Math.max(1, (hanzi || '').length / 2);
  return getPauseMs() * factor;
}}

async function start() {{
  const ws = words();
  if (ws.length === 0) return;
  if (state === 'playing') return;
  state = 'playing';
  paused = false;
  const myGen = ++runGen;
  setButtons(true);
  if (idx < 0 || idx >= ws.length) idx = 0;

  for (; idx < ws.length; idx++) {{
    if (myGen !== runGen) return;
    highlight(idx);
    updateStatus();
    await playAudio(ws[idx].audio_url);
    await waitWhilePaused();
    if (myGen !== runGen) return;
    if (idx < ws.length - 1) {{
      await sleepCancelable(pauseForWord(ws[idx].hanzi));
      await waitWhilePaused();
      if (myGen !== runGen) return;
      reveal(idx);
    }}
  }}
  await sleepCancelable(pauseForWord(ws[ws.length - 1].hanzi));
  if (myGen !== runGen) return;
  reveal(ws.length - 1);

  state = 'idle';
  idx = -1;
  updateStatus();
  highlight(-1);
  setButtons(false);
}}

function pauseToggle() {{
  paused = !paused;
  $('btn-pause').textContent = paused ? '▶ 繼續' : '⏸ 暫停';
  if (paused && !player.paused) player.pause();
  else if (!paused && player.paused && player.src) player.play();
}}

function skip() {{
  if (sleepCanceler) sleepCanceler();
  if (!player.paused) player.pause();
  if (audioResolver) audioResolver();
}}

function stop() {{
  runGen++;
  state = 'idle';
  paused = false;
  $('btn-pause').textContent = '⏸ 暫停';
  if (sleepCanceler) sleepCanceler();
  player.pause();
  if (audioResolver) audioResolver();
  setButtons(false);
}}

function reset() {{
  stop();
  idx = -1;
  updateStatus();
  highlight(-1);
  document.querySelectorAll('.card').forEach(c => c.classList.add('hidden'));
}}

// init
renderTabs();
renderContent();
</script>

</body>
</html>
"""


def render_html(spec: dict, date_str: str) -> str:
    lessons_for_js = [
        {
            "id": l["id"],
            "title": l["title"],
            "words": [
                {"hanzi": w["hanzi"], "pinyin": w.get("pinyin", ""), "audio_url": w["audio_url"]}
                for w in l.get("words", [])
            ],
        }
        for l in spec["lessons"]
    ]
    n_total = sum(len(l.get("words", [])) for l in spec["lessons"])
    return HTML_TEMPLATE.format(
        title=html.escape(spec["title"]),
        date=date_str,
        n_lessons=len(spec["lessons"]),
        n_total_words=n_total,
        repeat=spec["repeat"],
        pause=spec["pause_seconds"],
        lessons_json=json.dumps(lessons_for_js, ensure_ascii=False),
    )


def synth_lesson(lesson: dict, slug: str, voice: str, engine: str, repeat: int) -> int:
    """Generate (or reuse) audio for each word in a lesson. Returns count of new TTS calls."""
    assets_dir = WEB_ROOT / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    new_tts = 0
    for w in lesson.get("words", []):
        h = word_hash(w["hanzi"], voice, engine, repeat)
        audio_filename = f"dictation-{slug}-{h}.ogg"
        audio_dst = assets_dir / audio_filename
        if not audio_dst.exists():
            src = tts_word(w["hanzi"], repeat, voice, engine)
            shutil.move(str(src), audio_dst)
            new_tts += 1
        w["audio_url"] = f"{URL_PREFIX}/assets/{audio_filename}"
    return new_tts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input_json", help="Path to JSON spec file")
    args = ap.parse_args()

    spec = json.loads(Path(args.input_json).read_text(encoding="utf-8"))
    spec.setdefault("pause_seconds", 5)
    spec.setdefault("repeat", 2)
    spec.setdefault("engine", "gemini")
    spec.setdefault("voice", "erinome")
    spec.setdefault("title", "中文聽寫")
    spec.setdefault("slug", "")

    slug = slugify(spec["slug"])
    if not slug:
        print("ERROR: 'slug' is required for stable URLs (ASCII, e.g. 'exam-2026')", file=sys.stderr)
        return 1

    if not spec.get("lessons"):
        print("ERROR: 'lessons' list is required", file=sys.stderr)
        return 1

    total_words = sum(len(l.get("words", [])) for l in spec["lessons"])
    print(f"[info] {len(spec['lessons'])} lessons, {total_words} words total "
          f"({spec['engine']}/{spec['voice']}, slug={slug})", file=sys.stderr)

    total_new_tts = 0
    for lesson in spec["lessons"]:
        n_words = len(lesson.get("words", []))
        if n_words == 0:
            print(f"  [{lesson['id']}] {lesson['title']} — empty", file=sys.stderr)
            continue
        print(f"  [{lesson['id']}] {lesson['title']} — {n_words} words", file=sys.stderr)
        for i, w in enumerate(lesson["words"], 1):
            h = word_hash(w["hanzi"], spec["voice"], spec["engine"], spec["repeat"])
            cached = (WEB_ROOT / "assets" / f"dictation-{slug}-{h}.ogg").exists()
            print(f"    {i:2d}. {w['hanzi']:6s} {'(cached)' if cached else '(synth)'}",
                  file=sys.stderr)
        new = synth_lesson(lesson, slug, spec["voice"], spec["engine"], spec["repeat"])
        total_new_tts += new

    print(f"[info] {total_new_tts} new TTS calls (rest cached)", file=sys.stderr)

    html_content = render_html(spec, date_str=dt.datetime.now().strftime("%Y-%m-%d"))
    html_filename = f"dictation-{slug}.html"        # stable name — same URL every run
    html_dst = WEB_ROOT / "pages" / html_filename
    html_dst.parent.mkdir(parents=True, exist_ok=True)
    html_dst.write_text(html_content, encoding="utf-8")

    # Persist spec (without derived audio_url) so future runs/sessions can reload state
    spec_for_save = {k: v for k, v in spec.items() if k != "lessons"}
    spec_for_save["lessons"] = [
        {
            "id": l["id"],
            "title": l["title"],
            "words": [
                {kk: vv for kk, vv in w.items() if kk != "audio_url"}
                for w in l.get("words", [])
            ],
        }
        for l in spec["lessons"]
    ]
    spec_dst = WEB_ROOT / "assets" / f"dictation-{slug}.spec.json"
    spec_dst.write_text(
        json.dumps(spec_for_save, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    page_url = f"{URL_PREFIX}/pages/{html_filename}"
    print(f"[info] HTML written to {html_dst}", file=sys.stderr)
    print(f"[info] state saved to {spec_dst}", file=sys.stderr)
    print(page_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
