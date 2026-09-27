#!/usr/bin/env python3
"""一段录音里所有「整句漏念」自动补上：用 srt.py 同一套全局对齐找出时长几乎为零的句子，
再拿 whisper 的原始识别文本复核（短句如「为什么？」常被误判），确认真没念的才调 splice_missing.py 补。
用法：fix_missing.py s<N>.txt s<N>-words.json 录音.mp3     → 打印补了哪些；补过的录音要重跑时间戳与 srt。"""
import json, os, re, subprocess, sys
V = os.path.expanduser("~/.claude/skills/lecture-video/scripts")
sys.path.insert(0, V)
from tmap import norm, stream_of, script_times
txt, words, mp3 = sys.argv[1:4]
body = open(txt, encoding="utf-8").read()
raw = re.sub(r"\[[a-z]+\]\s*", "", body)
sents = [s.strip() for s in re.split(r"(?<=[。？！])", raw.replace("\n", "")) if s.strip()]
script, offs = "", []
for s in sents:
    offs.append(len(script)); script += norm(s)
stream, times = stream_of(words)
st = script_times(script, stream, times)
starts = [st[min(o, len(st) - 1)] for o in offs]
segs = json.load(open(words, encoding="utf-8"))
def heard(a, b):
    return norm("".join(w["w"] for s in segs for w in s["words"] if a - 2 <= w["s"] <= b + 2))
miss_idx = []
for i, s in enumerate(sents[:-1]):
    a, b = starts[i], starts[i + 1] - 0.05
    if (b - a) >= max(0.5, 0.06 * len(norm(s))) or i == 0:
        continue
    k = norm(s); h = heard(starts[i - 1], starts[i + 1])
    hit = sum(1 for c in set(k) if c in h) / max(1, len(set(k)))
    if hit >= 0.8 and len(k) <= 8:
        print("  误判跳过（识别里有）：", s); continue
    miss_idx.append(i)
# 连着漏的几句并成一段补：锚点是这一串前面第一句真念了的
todo, run = [], []
for i in miss_idx + [None]:
    if run and (i is None or i != run[-1] + 1):
        pos = body.find(sents[run[0]][:12]); tags = re.findall(r"\[([a-z]+)\]", body[:pos]); tag = tags[-1] if tags else "thinking"
        todo.append((sents[run[0] - 1], "[%s] %s" % (tag, "".join(sents[j] for j in run))))
        run = []
    if i is not None:
        run.append(i)
srt = "/tmp/fixmiss-%d.srt" % os.getpid()
subprocess.run([sys.executable, V + "/srt.py", txt, words, srt], capture_output=True)
# 从后往前插：前面的插入不影响后面句子的时间
for prev, miss in reversed(todo):
    subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "splice_missing.py"), mp3, srt, prev, miss], check=True)
print("补了 %d 句" % len(todo))
