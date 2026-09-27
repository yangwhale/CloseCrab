#!/usr/bin/env python3
"""TTS 偶尔整句漏念（srt.py 会报「录音里像是没念」）。只补这一句，不重录整段：
单独合成这一句（同一条合成路径），插在上一句结束的地方，前后各留 0.25 秒。
用法：splice_missing.py <录音.mp3> <字幕.srt> "<上一句原文>" "[标签] 漏掉的那句"
插完要对这段录音重跑逐字时间戳和 srt（后面的时间整体后移了）。"""
import os, re, subprocess, sys, tempfile
mp3, srt, prev, missing = sys.argv[1:5]
norm = lambda s: re.sub(r"[\s，。！？、：；,.!?\"“”「」]", "", s)
t_end = None
for blk in open(srt, encoding="utf-8").read().strip().split("\n\n"):
    L = blk.split("\n")
    if len(L) >= 3 and norm("".join(L[2:])) == norm(prev):
        h, m, s = L[1].split(" --> ")[1].replace(",", ".").split(":"); t_end = int(h) * 3600 + int(m) * 60 + float(s)
if t_end is None:
    sys.exit("字幕里找不到上一句：" + prev)
d = tempfile.mkdtemp()
open(d + "/m.txt", "w").write(missing)
subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "offline_tts.py"), d + "/m.txt", d + "/m.pcm"], check=True)
f = lambda *a: subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *a], check=True)
f("-f", "s16le", "-ar", "48000", "-ac", "2", "-i", d + "/m.pcm", "-af", "adelay=250|250,apad=pad_dur=0.25", "-ac", "1", "-ar", "24000", d + "/m.wav")
f("-i", mp3, "-t", str(t_end), "-ac", "1", "-ar", "24000", d + "/a.wav")
f("-ss", str(t_end), "-i", mp3, "-ac", "1", "-ar", "24000", d + "/b.wav")
open(d + "/l.txt", "w").write("file '%s/a.wav'\nfile '%s/m.wav'\nfile '%s/b.wav'\n" % (d, d, d))
f("-f", "concat", "-safe", "0", "-i", d + "/l.txt", "-ac", "1", "-b:a", "64k", mp3 + ".new.mp3")
os.replace(mp3 + ".new.mp3", mp3)
print("插在 %.2f 秒：%s" % (t_end, missing))
