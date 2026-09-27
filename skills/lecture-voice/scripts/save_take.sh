#!/usr/bin/env bash
# 把刚才那一轮现场讲课（飞书语音「重播」那段原声）存成 mp3，并用 STT 核对开头结尾。
# 用法：save_take.sh <输出.mp3> [开头剪掉秒数]
# 开头若有跟上课无关的交代（「上一节录音已放好」之类），先跑一次看 STT 打印的开头，再带剪掉秒数重跑。
set -euo pipefail
OUT="$1"; SKIP="${2:-0}"
PCM=$(ls -t /tmp/jarvis-tts-buf/*.pcm | head -1)
echo "取：$PCM（$(stat -c %y "$PCM" | cut -c1-19)）"
ffmpeg -y -loglevel error -f s16le -ar 48000 -ac 2 -ss "$SKIP" -i "$PCM" -ac 1 -b:a 64k "$OUT"
ffmpeg -y -loglevel error -i "$OUT" -t 6 -ac 1 -ar 16000 /tmp/lv-head.wav
ffmpeg -y -loglevel error -sseof -8 -i "$OUT" -ac 1 -ar 16000 /tmp/lv-tail.wav
cd ~/CloseCrab && python3 - <<'PY' 2>/dev/null | tail -2
import sys; sys.path.insert(0, '.')
from closecrab.utils.stt import STTEngine
e = STTEngine()
print("开头：", e.transcribe('/tmp/lv-head.wav'))
print("结尾：", e.transcribe('/tmp/lv-tail.wav'))
PY
echo "时长：$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT") 秒 → $OUT"
