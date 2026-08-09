#!/usr/bin/env python3
"""Generate a multi-lesson read-aloud HTML page (one tab per lesson).

Built for Hong Kong primary-school General-Studies-style lessons: the user
drops one PDF *per lesson*, Claude extracts the English text, and this script
turns each lesson into a tab containing:
  - a single <audio controls> player that reads the whole lesson aloud,
  - the original PDF page screenshots (so kids can follow the textbook),
  - the extracted text (large, clean, for reading along).

Stable per `slug`: re-running with the same slug **updates in place** (the
user's bookmark always points to the latest version). Audio is hash-named, so
re-runs that don't change a lesson's text/voice **skip TTS entirely** for that
lesson — perfect for incrementally adding lessons.

Input JSON shape:

    {
      "title": "P1 常識科朗讀",
      "slug": "gs-p1-unit1",
      "engine": "gemini",
      "voice": "kore",
      "lessons": [
        {
          "id": "L01",
          "title": "Lesson 1: My Body",
          "pdf": "/tmp/lesson1.pdf",          // optional — for page screenshots
          "paragraphs": [                       // the lesson text, one entry per paragraph
            "Our body has many parts.",
            "We use our eyes to see and our ears to hear."
          ]
        },
        {"id": "L02", "title": "Lesson 2: ...", "pdf": "...", "paragraphs": [...]}
      ]
    }

A lesson may use "text" (one string) instead of "paragraphs"; it is split on
blank lines into paragraphs. Output: prints the public CC Pages URL on stdout.
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

# Bumping this invalidates all cached audio (when the director prompt changes).
PROMPT_VERSION = "v1-teacher-readaloud"

# English director prompt — 0 square-bracket tags on purpose, so tts-generate.py
# passes it through verbatim (its single-tag mode hardcodes "Say ... in Chinese",
# which would force Mandarin mode on English text). Gemini TTS natively reads a
# natural-language style instruction followed by a colon + the transcript.
DIRECTOR = (
    "Read the following lesson aloud for a young primary-school child. "
    "Speak clearly and warmly, like a kind teacher, at a calm and gentle pace, "
    "with clear pronunciation and a natural short pause between sentences:"
)


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


def lesson_paragraphs(lesson: dict) -> list[str]:
    """Normalize a lesson's text into a list of paragraphs."""
    if lesson.get("paragraphs"):
        return [p.strip() for p in lesson["paragraphs"] if p.strip()]
    text = lesson.get("text", "")
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def text_hash(text: str, voice: str, engine: str) -> str:
    """Stable short hash — same text/voice/engine → same audio filename."""
    key = f"{PROMPT_VERSION}|{engine}|{voice}|{text}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def tts_lesson(paragraphs: list[str], voice: str, engine: str) -> Path:
    """Synthesize one whole lesson into a single OGG.

    Paragraphs are joined with blank lines so Gemini takes a natural breath
    between them. For Gemini we prepend the English DIRECTOR style prompt; Edge
    TTS gets the raw transcript (it has no style support).
    """
    transcript = "\n\n".join(paragraphs)
    text = f"{DIRECTOR}\n\n{transcript}" if engine == "gemini" else transcript
    res = run(["python3", str(TTS_SCRIPT), "--engine", engine, "--voice", voice, text])
    path = Path(res.stdout.strip().splitlines()[-1])
    if not path.exists():
        raise RuntimeError(f"TTS returned non-existent path: {path!r}")
    return path


def speed_up(src: Path, dst: Path, speed: float) -> None:
    """Time-stretch an OGG by `speed` (pitch preserved) via ffmpeg atempo.

    atempo accepts 0.5–2.0 in one pass; we clamp into range. Output re-encoded
    as Vorbis so the result is still a plain .ogg the <audio> player can stream.
    """
    speed = max(0.5, min(2.0, speed))
    run([
        "ffmpeg", "-y", "-i", str(src),
        "-filter:a", f"atempo={speed:g}",
        "-c:a", "libvorbis", "-q:a", "4",
        str(dst),
    ])


def render_pdf_pages(pdf_path: Path, out_dir: Path, prefix: str, dpi: int = 200) -> list[str]:
    """pdftoppm a lesson PDF to PNGs. Returns relative filenames (sorted)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_dir / prefix
    # Clear any stale pages for this prefix so a shorter re-run doesn't leave orphans.
    for old in out_dir.glob(f"{prefix}-*.png"):
        old.unlink()
    run(["pdftoppm", "-png", "-r", str(dpi), str(pdf_path), str(stem)])
    return sorted(p.name for p in out_dir.glob(f"{prefix}-*.png"))


HTML_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta property="og:title" content="{title}">
<meta property="og:description" content="{n_lessons} lessons · read-aloud">
<meta name="theme-color" content="#29b6f6">
<style>
  :root {{
    --bg:#0f1a20; --card:rgba(255,255,255,.06); --card-border:rgba(255,255,255,.12);
    --text:#e3f2fd; --text2:#90caf9; --accent:#29b6f6;
  }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; padding:18px 14px 70px; line-height:1.7;
    font-family:-apple-system,"Segoe UI","PingFang SC","Noto Sans CJK SC",sans-serif;
    background:var(--bg); color:var(--text);
    max-width:880px; margin-left:auto; margin-right:auto;
  }}
  header {{ text-align:center; margin-bottom:12px; }}
  h1 {{ margin:0 0 6px; font-size:22px; }}
  .meta {{ color:var(--text2); font-size:12px; }}
  input[name="tab"] {{ display:none; }}
  .tabs {{
    display:flex; gap:6px; overflow-x:auto; padding:4px 0 10px;
    border-bottom:1px solid var(--card-border); margin-bottom:6px;
    scrollbar-width:thin;
  }}
  .tabs label {{
    flex:0 0 auto; padding:8px 14px; cursor:pointer; font-size:13px;
    color:var(--text2); border-radius:8px 8px 0 0;
    border-bottom:3px solid transparent; white-space:nowrap; transition:all .15s;
  }}
  .tabs label:hover {{ background:var(--card); color:var(--text); }}
  .lesson {{ display:none; }}
{tab_css}
  .player {{
    position:sticky; top:0; z-index:5; background:var(--bg);
    padding:10px 0 12px; margin-bottom:6px;
  }}
  .player audio {{ width:100%; height:40px; }}
  .player .hint {{ color:var(--text2); font-size:12px; margin:4px 2px 0; }}
  .lesson h2 {{ font-size:19px; color:var(--accent); margin:6px 0 14px; }}
  .text p {{ margin:0 0 14px; font-size:18px; }}
  .sec-label {{
    color:var(--text2); font-size:12px; letter-spacing:.08em; text-transform:uppercase;
    margin:22px 0 8px; padding-top:14px; border-top:1px dashed var(--card-border);
  }}
  .page-img {{
    width:100%; border-radius:12px; margin:0 0 12px;
    border:1px solid var(--card-border); display:block;
  }}
  @media (max-width:600px) {{
    body {{ padding:12px 10px 50px; }}
    h1 {{ font-size:19px; }} .text p {{ font-size:17px; }}
  }}
  @media print {{
    body {{ background:#fff; color:#111; }}
    .tabs, input[name="tab"], .player {{ display:none !important; }}
    .lesson {{ display:block !important; page-break-after:always; }}
    .page-img {{ border:1px solid #ccc; }}
  }}
</style>
</head>
<body>
<header>
  <h1>{title}</h1>
  <div class="meta">{date} · {n_lessons} lessons</div>
</header>
{radios}
<div class="tabs">
{labels}
</div>
{lessons}
</body>
</html>
"""


def render_html(spec: dict, lessons: list[dict], date_str: str) -> str:
    radios, labels, blocks, css = [], [], [], []
    for i, l in enumerate(lessons):
        rid = f"tab-{l['id']}"
        checked = " checked" if i == 0 else ""
        radios.append(f'<input type="radio" name="tab" id="{rid}"{checked}>')
        labels.append(f'  <label for="{rid}">{html.escape(l["title"])}</label>')
        css.append(
            f'  #{rid}:checked ~ .tabs label[for="{rid}"]{{color:var(--accent);'
            f'border-bottom-color:var(--accent);background:var(--card);font-weight:600;}}\n'
            f'  #{rid}:checked ~ #lesson-{l["id"]}{{display:block;}}'
        )
        imgs = "".join(
            f'<img class="page-img" src="{u}" alt="{html.escape(l["title"])} page" loading="lazy">'
            for u in l["page_urls"]
        )
        paras = "".join(f"<p>{html.escape(p)}</p>" for p in l["paragraphs"])
        text_sec = f'<div class="sec-label">Lesson text</div><div class="text">{paras}</div>' if paras else ""
        img_sec = f'<div class="sec-label">Textbook pages</div>{imgs}' if imgs else ""
        blocks.append(
            f'<div class="lesson" id="lesson-{l["id"]}">\n'
            f'  <div class="player"><audio controls preload="none" src="{l["audio_url"]}"></audio>'
            f'<div class="hint">▶ 點擊播放，跟著朗讀</div></div>\n'
            f'  <h2>{html.escape(l["title"])}</h2>\n'
            f'  {text_sec}\n  {img_sec}\n'
            f'</div>'
        )
    return HTML_HEAD.format(
        title=html.escape(spec["title"]),
        date=date_str,
        n_lessons=len(lessons),
        tab_css="\n".join(css),
        radios="\n".join(radios),
        labels="\n".join(labels),
        lessons="\n".join(blocks),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input_json", help="Path to JSON spec file")
    args = ap.parse_args()

    spec = json.loads(Path(args.input_json).read_text(encoding="utf-8"))
    spec.setdefault("engine", "gemini")
    spec.setdefault("voice", "kore")
    spec.setdefault("title", "Read-Aloud Lesson")
    spec.setdefault("slug", "")

    slug = slugify(spec["slug"])
    if not slug:
        print("ERROR: 'slug' is required for stable URLs (ASCII, e.g. 'gs-p1-unit1')", file=sys.stderr)
        return 1
    if not spec.get("lessons"):
        print("ERROR: 'lessons' list is required", file=sys.stderr)
        return 1

    engine, voice = spec["engine"], spec["voice"]
    speed = float(spec.get("speed", 1.0))
    assets_dir = WEB_ROOT / "assets" / f"lra-{slug}"
    assets_dir.mkdir(parents=True, exist_ok=True)

    print(f"[info] {len(spec['lessons'])} lessons ({engine}/{voice}, speed={speed:g}x, slug={slug})", file=sys.stderr)

    rendered, new_tts = [], 0
    for l in spec["lessons"]:
        lid, ltitle = l["id"], l.get("title", l["id"])
        paras = lesson_paragraphs(l)
        if not paras:
            print(f"  [{lid}] {ltitle} — WARNING: no text, skipping audio", file=sys.stderr)

        # Audio (hash-cached on full lesson text).
        audio_url = ""
        if paras:
            full = "\n\n".join(paras)
            h = text_hash(full, voice, engine)
            audio_name = f"{lid}-{h}.ogg"
            audio_dst = assets_dir / audio_name
            if audio_dst.exists():
                print(f"  [{lid}] {ltitle} — {len(paras)} paras (audio cached)", file=sys.stderr)
            else:
                print(f"  [{lid}] {ltitle} — {len(paras)} paras (synth)", file=sys.stderr)
                src = tts_lesson(paras, voice, engine)
                shutil.move(str(src), audio_dst)
                new_tts += 1
            # Optional time-stretch. Base (1.0x) OGG stays cached on text+voice;
            # the sped-up copy gets its own name so re-runs reuse the base TTS.
            if abs(speed - 1.0) > 1e-3:
                fast_name = f"{lid}-{h}-s{int(round(speed * 100))}.ogg"
                fast_dst = assets_dir / fast_name
                if fast_dst.exists():
                    print(f"        {speed:g}x audio cached", file=sys.stderr)
                else:
                    print(f"        building {speed:g}x audio", file=sys.stderr)
                    speed_up(audio_dst, fast_dst, speed)
                audio_name = fast_name
            audio_url = f"{URL_PREFIX}/assets/lra-{slug}/{audio_name}"

        # Page screenshots (re-rendered each run; cheap and keeps them fresh).
        page_urls = []
        pdf = l.get("pdf")
        if pdf and Path(pdf).exists():
            names = render_pdf_pages(Path(pdf), assets_dir, prefix=lid)
            page_urls = [f"{URL_PREFIX}/assets/lra-{slug}/{n}" for n in names]
            print(f"        {len(page_urls)} page image(s)", file=sys.stderr)
        elif pdf:
            print(f"        WARNING: pdf not found: {pdf}", file=sys.stderr)

        rendered.append({
            "id": lid, "title": ltitle,
            "paragraphs": paras, "audio_url": audio_url, "page_urls": page_urls,
        })

    print(f"[info] {new_tts} new TTS call(s) (rest cached)", file=sys.stderr)

    html_content = render_html(spec, rendered, dt.datetime.now().strftime("%Y-%m-%d"))
    html_name = f"lesson-{slug}.html"            # stable name — same URL every run
    html_dst = WEB_ROOT / "pages" / html_name
    html_dst.parent.mkdir(parents=True, exist_ok=True)
    html_dst.write_text(html_content, encoding="utf-8")

    page_url = f"{URL_PREFIX}/pages/{html_name}"
    print(f"[info] HTML written to {html_dst}", file=sys.stderr)
    print(page_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
