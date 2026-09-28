#!/usr/bin/env python3
"""横屏讲课视频 → 竖屏短片（1080×1920）：顶部标题，中间课件画面（裁掉两侧留白放大），底部大字幕，结尾 CTA。
用法：make_short.py 全片.mp4 全片.srt out.mp4 "段1起-止,段2起-止" "标题行1|标题行2" "结尾引导" [裁剪x,宽]"""
import re, subprocess, sys, tempfile, os
src, srt, out, segs, title, cta = sys.argv[1:7]
cx, cw = (sys.argv[7].split(",") if len(sys.argv) > 7 else ("820", "2200"))
segs = [tuple(map(float, s.split("-"))) for s in segs.split(",")]
d = tempfile.mkdtemp()
tp = lambda t: sum(float(x) * m for x, m in zip(t.replace(",", ".").split(":"), (3600, 60, 1)))
cues = []
for b in open(srt, encoding="utf-8").read().strip().split("\n\n"):
    L = b.split("\n"); a, e = L[1].split(" --> "); cues.append((tp(a), tp(e), "".join(L[2:])))
# 字幕：按片段重映射时间
ass = ["[Script Info]\nPlayResX: 1080\nPlayResY: 1920\n\n[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,OutlineColour,BackColour,Bold,Alignment,MarginL,MarginR,MarginV,BorderStyle,Outline,Shadow\n"
       "Style: S,Noto Sans CJK SC,64,&H00202124,&H00FFFFFF,&H00FFFFFF,1,8,70,70,1490,1,0,0\n"
       "Style: C,Noto Sans CJK SC,58,&H00FFFFFF,&H00E8731A,&H00E8731A,1,2,60,60,60,3,18,0\n\n[Events]\nFormat: Layer,Start,End,Style,Text\n"]
fmt = lambda t: "%d:%02d:%05.2f" % (t // 3600, t % 3600 // 60, t % 60)
off = 0.0
for a, b in segs:
    for s, e, t in cues:
        if e <= a or s >= b: continue
        ss, ee = max(s, a) - a + off, min(e, b) - a + off
        t = re.sub(r"(.{15})", r"\1\\N", t) if len(t) > 15 else t
        ass.append("Dialogue: 0,%s,%s,S,%s\n" % (fmt(ss), fmt(ee), t))
    off += b - a
ass.append("Dialogue: 0,%s,%s,C,%s\n" % (fmt(max(0, off - 6)), fmt(off), cta))
open(d + "/s.ass", "w", encoding="utf-8").write("".join(ass))
parts = []
for i, (a, b) in enumerate(segs):
    p = "%s/p%d.mp4" % (d, i)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(a), "-to", str(b), "-i", src, "-map", "0:v", "-map", "0:a",
                    "-c:v", "libx264", "-crf", "16", "-preset", "fast", "-c:a", "aac", "-b:a", "160k", p], check=True)
    parts.append(p)
open(d + "/l.txt", "w").write("".join("file '%s'\n" % p for p in parts))
t1, t2 = (title.split("|") + [""])[:2]
esc = lambda s: s.replace(":", r"\:").replace("'", "’")
vf = ("[0:v]crop=%s:2160:%s:0,scale=1080:-2[v];color=c=white:s=1080x1920[bg];[bg][v]overlay=0:370:shortest=1[o];"
      "[o]drawbox=x=0:y=0:w=1080:h=18:color=0x4285F4:t=fill,"
      "drawtext=fontfile=/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc:text='%s':fontsize=40:fontcolor=0x5F6368:x=70:y=90,"
      "drawtext=fontfile=/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc:text='%s':fontsize=64:fontcolor=0x202124:x=70:y=160,"
      "drawtext=fontfile=/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc:text='%s':fontsize=64:fontcolor=0x1A73E8:x=70:y=250,"
      "ass=%s/s.ass[out]") % (cw, cx, esc("现代 AI 加速器 GPU / TPU 系统课程"), esc(t1), esc(t2), d)
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", d + "/l.txt", "-filter_complex", vf, "-map", "[out]", "-map", "0:a",
                "-c:v", "libx264", "-crf", "18", "-preset", "slow", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out], check=True)
print(out, round(off, 1), "s")
