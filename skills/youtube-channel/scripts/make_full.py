#!/usr/bin/env python3
"""把一课各段 4K 成片拼成整片，合并字幕、生成章节表。
用法：make_full.py <段目录> <课前缀如 topic01> <字幕目录> "s0=标题|s1=标题|..." """
import subprocess, sys, os
d, pfx, srtdir, spec = sys.argv[1:5]
segs = [x.split("=", 1) for x in spec.split("|")]
dur = lambda f: float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", f], capture_output=True, text=True).stdout)
ts = lambda s: "%02d:%02d:%02d,%03d" % (s // 3600, s % 3600 // 60, s % 60, round((s - int(s)) * 1000) % 1000)
tp = lambda t: sum(float(x) * m for x, m in zip(t.replace(",", ".").split(":"), (3600, 60, 1)))
mm = lambda s: "%d:%02d" % (s // 60, s % 60) if s < 3600 else "%d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60)
# 各段音频参数可能不一（补录过的段是 24 kHz 单声道），先统一成 48 kHz 双声道再拼，否则 concat 的音轨时间会乱
for i, _ in segs:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", "%s/%s-%s-4k.mp4" % (d, pfx, i), "-map", "0:v", "-map", "0:a", "-c:v", "copy",
                    "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "160k", "%s/%s-%s-n.mp4" % (d, pfx, i)], check=True)
open(d + "/cat.txt", "w").write("".join("file '%s/%s-%s-n.mp4'\n" % (d, pfx, i) for i, _ in segs))
full = "%s/%s-full-4k.mp4" % (d, pfx)
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", d + "/cat.txt", "-map", "0:v", "-map", "0:a", "-c", "copy", "-movflags", "+faststart", full], check=True)
off, out, ch, k = 0.0, [], [], 1
for i, title in segs:
    ch.append("%s %s" % (mm(int(off)), title))
    for blk in open("%s/%s.srt" % (srtdir, i), encoding="utf-8").read().strip().split("\n\n"):
        L = blk.split("\n"); a, b = L[1].split(" --> ")
        out.append("%d\n%s --> %s\n%s" % (k, ts(tp(a) + off), ts(tp(b) + off), "\n".join(L[2:]))); k += 1
    off += dur("%s/%s-%s-4k.mp4" % (d, pfx, i))
open("%s/%s-full.srt" % (d, pfx), "w", encoding="utf-8").write("\n\n".join(out) + "\n")
open("%s/%s-chapters.txt" % (d, pfx), "w").write("\n".join(ch) + "\n")
print("\n".join(ch)); print("total", mm(int(off)), "file", mm(int(dur(full))), os.path.getsize(full) // 2**20, "MB")
