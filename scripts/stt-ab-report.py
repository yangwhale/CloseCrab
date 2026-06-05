#!/usr/bin/env python3
"""STT A/B 测试报告生成器。
读取 sidecar 的 _stt_ab_results，生成 HTML 报告 + 上传 WAV 到 CC Pages。
用法: python3 scripts/stt-ab-report.py [--upload]
"""
import json, os, sys, time, subprocess, shutil
from pathlib import Path

CC_PAGES_ROOT = os.environ.get("CC_PAGES_WEB_ROOT", os.path.expanduser("~/gcs-mount/cc-pages"))
CC_PAGES_URL = os.environ.get("CC_PAGES_URL_PREFIX", "https://cc.higcp.com")


def load_results_from_log(log_path: str = None):
    """从 bot.log 解析 [STT-AB] 日志重建结果（fallback 方案）。"""
    if log_path is None:
        log_path = os.path.expanduser("~/.claude/closecrab/jarvis/bot.log")
    import re
    results = []
    current = None
    for line in open(log_path):
        if "[STT-AB] 录音落盘:" in line:
            m = re.search(r"seq=(\d+) (.+?) \((.+?)s\)", line)
            if m:
                current = {
                    "seq": int(m.group(1)),
                    "wav_path": m.group(2),
                    "audio_dur": float(m.group(3)),
                    "chirp3": {"text": "", "t": 0},
                    "funasr_online": {"text": "", "t": 0},
                    "funasr_offline": {"text": "", "t": 0},
                    "gemini": {"text": "", "t": 0},
                }
                results.append(current)
        elif "[STT-AB] Chirp3 final:" in line and current:
            m = re.search(r"t=([\d.]+) text='(.+?)'", line)
            if m:
                current["chirp3"] = {"text": m.group(2), "t": float(m.group(1))}
        elif "[STT-AB] FunASR 2pass-offline:" in line and current:
            m = re.search(r"t=([\d.]+) text='(.+?)'", line)
            if m:
                current["funasr_offline"] = {"text": m.group(2), "t": float(m.group(1))}
        elif "[STT-AB] FunASR 2pass-online:" in line and current:
            m = re.search(r"t=([\d.]+) text='(.+?)'", line)
            if m:
                current["funasr_online"] = {"text": m.group(2), "t": float(m.group(1))}
        elif "[STT-AB] Gemini final:" in line and current:
            m = re.search(r"t=([\d.]+).*text='(.+?)'", line)
            if m:
                current["gemini"] = {"text": m.group(2), "t": float(m.group(1))}
    return results


def upload_wavs(results, dest_dir="assets/stt-ab"):
    """上传 WAV 文件到 CC Pages。"""
    full_dest = os.path.join(CC_PAGES_ROOT, dest_dir)
    os.makedirs(full_dest, exist_ok=True)
    for r in results:
        src = r["wav_path"]
        if not os.path.exists(src):
            continue
        fname = os.path.basename(src)
        dst = os.path.join(full_dest, fname)
        shutil.copy2(src, dst)
        r["wav_url"] = f"{CC_PAGES_URL}/{dest_dir}/{fname}"
    print(f"上传 {len(results)} 个 WAV 到 {full_dest}")


def generate_html(results):
    """生成 HTML 报告。"""
    now = time.strftime("%Y%m%d-%H%M%S")
    rows = []
    for r in results:
        wav_url = r.get("wav_url", "")
        audio_tag = f'<audio controls preload="none" src="{wav_url}"></audio>' if wav_url else "(无音频)"
        dur = f'{r.get("audio_dur", 0):.1f}s'

        chirp3 = r.get("chirp3", {})
        funasr_on = r.get("funasr_online", {})
        funasr_off = r.get("funasr_offline", {})
        gemini = r.get("gemini", {})

        base_t = chirp3.get("t", 0)
        def delta(d):
            t = d.get("t", 0)
            if t and base_t:
                return f'{(t - base_t)*1000:+.0f}ms'
            return "-"

        rows.append(f"""
        <tr>
          <td>{r['seq']}</td>
          <td>{audio_tag}<br><small>{dur}</small></td>
          <td class="text">{chirp3.get('text','')}<br><small class="latency">基准</small></td>
          <td class="text">{funasr_on.get('text','')}<br><small class="latency">{delta(funasr_on)}</small></td>
          <td class="text">{funasr_off.get('text','')}<br><small class="latency">{delta(funasr_off)}</small></td>
          <td class="text">{gemini.get('text','')}<br><small class="latency">{delta(gemini)}</small></td>
        </tr>""")

    html = f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>STT A/B 测试报告 — {now}</title>
<style>
  body {{ font-family: 'Google Sans', system-ui, sans-serif; margin: 2rem; background: #fafafa; }}
  h1 {{ color: #1a73e8; }}
  table {{ border-collapse: collapse; width: 100%; background: white; box-shadow: 0 1px 3px rgba(0,0,0,.12); }}
  th {{ background: #1a73e8; color: white; padding: 12px 8px; text-align: left; }}
  td {{ padding: 10px 8px; border-bottom: 1px solid #e0e0e0; vertical-align: top; }}
  td.text {{ max-width: 250px; font-size: 14px; }}
  tr:nth-child(even) {{ background: #f5f5f5; }}
  .latency {{ color: #5f6368; }}
  audio {{ width: 200px; }}
  .summary {{ margin-top: 2rem; padding: 1rem; background: #e8f0fe; border-radius: 8px; }}
</style>
</head>
<body>
<h1>STT A/B 测试报告</h1>
<p>生成时间: {time.strftime('%Y-%m-%d %H:%M:%S HKT')}</p>
<p>共 {len(results)} 组测试 · 三路对比: Chirp 3 (Google) / FunASR (阿里) / Gemini 3 Flash</p>
<table>
<tr>
  <th>#</th>
  <th>音频</th>
  <th>Chirp 3<br><small>(基准)</small></th>
  <th>FunASR Online</th>
  <th>FunASR Offline</th>
  <th>Gemini 3 Flash</th>
</tr>
{''.join(rows)}
</table>
<div class="summary">
  <h3>延迟说明</h3>
  <p>延迟为相对 Chirp 3 final transcript 的时间差。负值 = 比 Chirp 3 快，正值 = 比 Chirp 3 慢。</p>
  <p>Chirp 3: LiveKit 封装的 Google Cloud Speech V2 streaming。FunASR: 本地 Docker，2pass (online + offline)。Gemini: 整段 WAV 批量上传 generate_content。</p>
</div>
</body></html>"""

    out_name = f"stt-ab-report-{now}.html"
    out_path = os.path.join(CC_PAGES_ROOT, "pages", out_name)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        f.write(html)
    url = f"{CC_PAGES_URL}/pages/{out_name}"
    print(f"报告已生成: {url}")
    return url


if __name__ == "__main__":
    results = load_results_from_log()
    if not results:
        print("没有找到 STT-AB 测试数据。请先在 Discord 语音频道说几句话。")
        sys.exit(1)
    print(f"找到 {len(results)} 组测试数据")
    if "--upload" in sys.argv:
        upload_wavs(results)
    url = generate_html(results)
    print(f"\n完成！报告: {url}")
